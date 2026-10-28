"""SQLite schema + connection helpers.

Raw sqlite3, no ORM (matches morgan_septic). DDL lives here as module string constants and is
applied idempotently by init_db(). The catalog is the tool's source of truth; the xlsx export is
just a view of it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
-- Approved Insurance Providers (from the live RMA AIP API).
CREATE TABLE IF NOT EXISTS aips (
    aip_code        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_company  TEXT,
    agreement_type  TEXT,
    reinsurance_year INTEGER,
    city            TEXT,
    state           TEXT,
    phone           TEXT,
    website         TEXT,
    source_url      TEXT,
    fetched_at      TEXT
);

-- One row per product. bucket = '508h' (privately-developed federal plan, offered by all AIPs)
-- or 'private' (truly private, non-reinsured, varies by AIP).
CREATE TABLE IF NOT EXISTS products (
    product_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket          TEXT NOT NULL,
    program         TEXT,              -- federal_508h | private_nonreinsured
    name            TEXT NOT NULL,
    aip_code        TEXT,              -- NULL for federal plans (all AIPs); set for private products
    developer       TEXT,              -- 508(h) submitter / developing company
    plan_code       TEXT,              -- RMA insurance plan code, when known
    peril_type      TEXT,              -- e.g. hail, wind, margin, revenue, multi-peril
    coverage_type   TEXT,              -- e.g. named-peril, supplemental, gap, area
    status          TEXT,
    effective_date  TEXT,
    source_type     TEXT NOT NULL,     -- rma_api | rma_plans_seed | serff | aip_site | rma_adm
    source_url      TEXT,
    doc_url         TEXT,              -- brochure / filing document
    filing_id       TEXT,              -- SERFF filing id, when applicable
    verified        INTEGER DEFAULT 0, -- 1 = attributes confirmed against a primary source
    notes           TEXT,
    raw             TEXT,              -- original JSON/text blob for provenance
    first_seen      TEXT,
    fetched_at      TEXT,
    -- Dedup key. A single column (not a multi-col UNIQUE) because SQLite treats NULLs as distinct,
    -- which would let rows with NULL plan_code/filing_id duplicate. Built by models.product_key().
    natural_key     TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS product_crops (
    product_id      INTEGER NOT NULL,
    crop            TEXT NOT NULL,
    PRIMARY KEY (product_id, crop),
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_states (
    product_id      INTEGER NOT NULL,
    state           TEXT NOT NULL,
    PRIMARY KEY (product_id, state),
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

-- County-grain availability (federal products only — private products are filed statewide, so
-- their grain is product_states). Populated by the rma_adm connector from ADM county lists.
CREATE TABLE IF NOT EXISTS product_counties (
    product_id      INTEGER NOT NULL,
    crop            TEXT NOT NULL,
    state           TEXT NOT NULL,     -- 2-letter code
    county_fips     TEXT NOT NULL,     -- 5-digit FIPS (state+county)
    county_name     TEXT,
    source          TEXT,              -- e.g. adm_2026
    PRIMARY KEY (product_id, crop, county_fips),
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

-- PRF (Pasture, Rangeland, Forage — rainfall index, plan code 13) County Base Values: the RMA
-- per-acre dollar value by county that scales protection (acres x productivity factor x coverage
-- level x CBV). County-level (the rainfall index/rates are grid-level, but the CBV is by county).
-- Varies by intended use (Grazing vs Haying) and practice (Irrigated vs Non-Irrigated, and
-- organic where applicable). Populated by the prf_adm connector from the ADM. Drives the app's
-- PRF county heat map. NOT part of the row-crop private-products catalog — a companion dataset.
CREATE TABLE IF NOT EXISTS prf_county (
    year               INTEGER NOT NULL,
    state              TEXT NOT NULL,     -- 2-letter USPS
    county_fips        TEXT NOT NULL,     -- 5-digit FIPS
    county_name        TEXT,
    intended_use       TEXT NOT NULL,     -- Grazing | Haying
    irrigation_practice TEXT NOT NULL,    -- Irrigated | Non-Irrigated
    organic_practice   TEXT NOT NULL DEFAULT 'Conventional',  -- Conventional | Organic | Transitional
    county_base_value  REAL,              -- $/acre
    source             TEXT,              -- e.g. adm_2026
    fetched_at         TEXT,
    PRIMARY KEY (year, county_fips, intended_use, irrigation_practice, organic_practice)
);

-- Market reality (Summary of Business): which FEDERAL plans actually SOLD, WHERE, at what
-- COVERAGE LEVEL, and what they PAID BACK. Populated by the rma_sob connector from RMA's public
-- "Crop Insurance Experience with Coverage Level" file (sobcov_<year>.zip, 1989 forward), at one
-- row per year x state x county x crop x plan x coverage-type x coverage-level.
--
-- ALL plan codes are loaded — the base row-crop plans (01 YP, 02 RP, 03 RPHPE, 90 APH and the
-- older 44/45 RA/CRC lineage) are the bulk of the market and used to be missing because the
-- connector filtered rows through the catalog's 508(h)-only products table. Joins the catalog's
-- federal products at query time via products.plan_code (semicolon lists), for the subset that
-- has a catalog product at all.
--
-- The public file carries no AIP/company identifier, so this is PLAN grain, not AIP grain (RMA
-- does not publish AIP-identified SoB at any granularity). net_acres = insured acres (base plans
-- report in Net Reported Quantity, endorsements like SCO/STAX/ECO/MCO in Endorsed/Companion
-- Acres); the connector takes whichever applies. producer_premium = total_premium - subsidy, the
-- denominator for the per-$1-of-own-money normalization the PRF and LRP work uses.
--
-- SIZE: millions of rows across the full history. scripts/build_app_db.py DROPS this table from
-- the shipped app DB; the app reads the national rollup (sob_national) instead.
CREATE TABLE IF NOT EXISTS sob_sales (
    year            INTEGER NOT NULL,
    state           TEXT NOT NULL,     -- 2-letter USPS
    county_fips     TEXT NOT NULL,     -- 5-digit FIPS (state+county)
    crop            TEXT NOT NULL,     -- canonical catalog crop (same names as product_crops)
    commodity_code  TEXT,              -- 4-digit RMA commodity code
    plan_code       TEXT NOT NULL,     -- 2-digit RMA insurance plan code
    plan_abbrev     TEXT,              -- e.g. RP, YP, APH, SCO-RP, ECO-YP
    coverage_type   TEXT NOT NULL,     -- A = buy-up, C = CAT, E = existing policy, L = limited
    coverage_level  REAL NOT NULL,     -- 0.50 .. 0.95 (0 when the plan carries no level)
    net_acres       REAL,
    liability       REAL,
    total_premium   REAL,
    subsidy         REAL,
    producer_premium REAL,             -- total_premium - subsidy (what the farmer paid)
    indemnity       REAL,
    policies_sold   INTEGER,
    policies_earning_premium INTEGER,
    policies_indemnified     INTEGER,
    units_earning_premium    INTEGER,
    units_indemnified        INTEGER,
    source          TEXT,              -- e.g. sobcov_2024
    fetched_at      TEXT,
    PRIMARY KEY (year, state, county_fips, crop, plan_code, coverage_type, coverage_level)
);
CREATE INDEX IF NOT EXISTS idx_sob_sales_year_plan ON sob_sales (year, plan_code);
CREATE INDEX IF NOT EXISTS idx_sob_sales_state ON sob_sales (state, crop, year);

-- National rollup of sob_sales, at year x crop x plan x coverage-type x coverage-level. Tens of
-- thousands of rows, so this is the Summary-of-Business table that SHIPS in data/catalog_app.db.
-- The two ratios are stored so the app never has to recompute them:
--   loss_ratio                    = indemnity / total_premium        (the industry ratio)
--   indemnity_per_producer_dollar = indemnity / (total_premium - subsidy)
-- The second is the row-crop analogue of the per-$1 normalization the PRF/LRP work uses: dollars
-- back per dollar of the producer's OWN premium. Both are NULL when the denominator is 0.
CREATE TABLE IF NOT EXISTS sob_national (
    year            INTEGER NOT NULL,
    crop            TEXT NOT NULL,
    commodity_code  TEXT,
    plan_code       TEXT NOT NULL,
    plan_abbrev     TEXT,
    coverage_type   TEXT NOT NULL,
    coverage_level  REAL NOT NULL,
    net_acres       REAL,
    liability       REAL,
    total_premium   REAL,
    subsidy         REAL,
    producer_premium REAL,
    indemnity       REAL,
    policies_sold   INTEGER,
    policies_earning_premium INTEGER,
    policies_indemnified     INTEGER,
    units_earning_premium    INTEGER,
    units_indemnified        INTEGER,
    loss_ratio      REAL,
    indemnity_per_producer_dollar REAL,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (year, crop, plan_code, coverage_type, coverage_level)
);

-- The UNIT STRUCTURE dial, which the coverage-level file does not carry. Populated from RMA's
-- "Coverage Level / Type / Practice / Unit Structure" file (sobtpu_<year>.zip), which begins in
-- 1999 — RMA publishes NO unit structure before that, and this file in turn publishes no policy
-- or unit counts. Aggregated to STATE grain (county x unit-structure would be ~5M rows for a
-- dimension that is analysed regionally). unit_structure is OU / BU / EU / WU / UA / UD, plus
-- EP / EC (enterprise unit by practice / by crop) and the placeholder NA RMA used through 2001.
-- SIZE: 300k rows costs ~45 MB, which alone would push data/catalog_app.db past GitHub's limit,
-- so build_app_db.py drops this one too and ships its national rollup (sob_unit_national) below.
CREATE TABLE IF NOT EXISTS sob_unit (
    year            INTEGER NOT NULL,
    state           TEXT NOT NULL,
    crop            TEXT NOT NULL,
    commodity_code  TEXT,
    plan_code       TEXT NOT NULL,
    plan_abbrev     TEXT,
    coverage_type   TEXT NOT NULL,
    coverage_level  REAL NOT NULL,
    unit_structure  TEXT NOT NULL,     -- OU, BU, EU, WU, UA, UD
    net_acres       REAL,
    liability       REAL,
    total_premium   REAL,
    subsidy         REAL,
    producer_premium REAL,
    indemnity       REAL,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (year, state, crop, plan_code, coverage_type, coverage_level, unit_structure)
);

-- National rollup of sob_unit (states summed away), carrying the same two ratios as
-- sob_national. ~40k rows, so this is the unit-structure table that SHIPS. It is derived from
-- sob_unit by plain SUM, so the two always agree; nothing here is independently sourced.
CREATE TABLE IF NOT EXISTS sob_unit_national (
    year            INTEGER NOT NULL,
    crop            TEXT NOT NULL,
    commodity_code  TEXT,
    plan_code       TEXT NOT NULL,
    plan_abbrev     TEXT,
    coverage_type   TEXT NOT NULL,
    coverage_level  REAL NOT NULL,
    unit_structure  TEXT NOT NULL,
    net_acres       REAL,
    liability       REAL,
    total_premium   REAL,
    subsidy         REAL,
    producer_premium REAL,
    indemnity       REAL,
    loss_ratio      REAL,
    indemnity_per_producer_dollar REAL,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (year, crop, plan_code, coverage_type, coverage_level, unit_structure)
);

-- One row per crop year loaded: the load manifest and the smoke test in one place. `settled` is
-- 0 for a crop year whose losses are still developing. That is not just the unpriced current
-- year: claims keep being adjusted and paid for well over a year, so the two newest crop years
-- always read far too clean (2026 loaded at a 0.08 loss ratio and 2025 at 0.55 against a mature
-- 0.91-0.93). Every realized-return query must exclude the open years — see
-- rma_sob._settled_clause, and rma_sob.mark_settled_years for how the flag is set.
CREATE TABLE IF NOT EXISTS sob_year (
    year             INTEGER PRIMARY KEY,
    sob_sales_rows   INTEGER,
    sob_national_rows INTEGER,
    sob_unit_rows    INTEGER,
    plans            INTEGER,          -- distinct plan codes present that year
    liability        REAL,
    total_premium    REAL,
    subsidy          REAL,
    indemnity        REAL,
    loss_ratio       REAL,
    settled          INTEGER,          -- 1 once indemnities exist for the year
    source           TEXT,
    fetched_at       TEXT
);

-- Row-crop supplemental-band OPPORTUNITY, precomputed per county x crop x band by
-- src/rowcropopt.py and read by src/rowcroppage.py. The exact call prf_opt_best makes: the
-- only county-grain source is sob_sales (3.23M rows, ~400 MB), scripts/build_app_db.py DROPS
-- it from the shipped app DB, so the map cannot compute this at runtime and reads this
-- compact result table instead. ~3k counties x a handful of crops x 4 bands.
--
-- THE METRIC, in one line:
--     unclaimed_subsidy = base_acres x (1 - penetration) x sub_per_acre
-- i.e. federal dollars available on acres that could carry a supplemental band (SCO / ECO /
-- MCO / STAX) and do not. base_acres is the ELIGIBLE denominator: individual plans (01 YP,
-- 02 RP, 03 RPHPE, 90 APH) at ADDITIONAL coverage only — CAT cannot carry a band, and no band
-- layers on an area or standalone-margin plan. penetration is capped at 1.0 and pen_capped
-- marks every cell where the raw ratio exceeded it.
--
-- WHAT IS COMPUTED AND WHAT IS FITTED. sub_per_acre / prem_per_acre come from the county's
-- OWN rows when it sells the band (value_basis 'county'); a county with no band sales has no
-- such figure, so they are fitted from base liability per acre via a per-(crop, band) ratio
-- at state then national scope (value_basis 'state' / 'national', 'mixed' on a rollup row
-- built from both). Nothing here is a blind national average silently applied locally.
--
-- evidence records how strong the claim that the band is even OFFERED is: 2 = sold for this
-- crop in THIS county, 1 = somewhere in this STATE, 0 = only elsewhere in the nation. Pairs
-- with no national sales at all get no row. crop = '(all crops)' is the rollup, summed over
-- every crop the band is offered on (so it is complete even though per-crop rows are capped
-- at the biggest crops); its penetration is acre-weighted.
CREATE TABLE IF NOT EXISTS rowcrop_unclaimed (
    year             INTEGER NOT NULL,
    state            TEXT,
    county_fips      TEXT NOT NULL,
    crop             TEXT NOT NULL,     -- canonical SoB crop name, or '(all crops)'
    band             TEXT NOT NULL,     -- SCO | ECO | MCO | STAX
    base_acres       REAL,              -- eligible denominator (see above)
    base_liability   REAL,
    base_policies    INTEGER,           -- SoB policy count, summed over base plans
    band_acres       REAL,
    penetration      REAL,              -- band_acres / base_acres, capped at 1.0
    pen_capped       INTEGER,           -- 1 when the raw ratio exceeded 1.0
    unsold_acres     REAL,              -- base_acres x (1 - penetration)
    sub_per_acre     REAL,              -- federal subsidy captured on one band acre
    prem_per_acre    REAL,              -- TOTAL premium on one band acre (the agency's base)
    pprem_per_acre   REAL,              -- producer's own share of that premium
    return_per_dollar REAL,             -- prem/pprem = 1/(1 - subsidy share)
    value_basis      TEXT,              -- county | state | national | mixed
    evidence         INTEGER,           -- 2 county · 1 state · 0 national-only
    unclaimed_subsidy REAL,             -- unsold_acres x sub_per_acre  <- THE METRIC
    unclaimed_premium REAL,             -- unsold_acres x prem_per_acre <- the agency's side
    source           TEXT,
    fetched_at       TEXT,
    PRIMARY KEY (year, county_fips, crop, band)
);

CREATE INDEX IF NOT EXISTS idx_rowcrop_unclaimed_pick
    ON rowcrop_unclaimed (year, crop, band, evidence);

-- PRF optimizer sweep results (grid grain): the BEST allocations found by src/prfopt.py's
-- 59,536-policy enumeration, summarized per grid x use x coverage level. Metrics are stored
-- NORMALIZED per $1 of annual protection (win rate is scale-invariant; $/acre = value x
-- county CBV x coverage x productivity, applied at display time), so one grid row serves
-- every county the grid touches. combos/props are JSON. Populated by src/prfsweep.py.
CREATE TABLE IF NOT EXISTS prf_opt_best (
    grid_id         INTEGER NOT NULL,
    intended_use    TEXT NOT NULL,
    coverage_level  REAL NOT NULL,
    -- The maximum percent of value one index interval may carry. It is ACTUARIAL (published
    -- per offer in ADM A01210, harvested to prf_max_pct), not a national constant: RY2026
    -- runs 40/45/50/60/70 and nine states are not uniform across their own counties. It is
    -- part of the KEY because it defines the legal policy universe, so two counties under one
    -- grid with different caps have genuinely different best policies. Only 617 of 13,462
    -- grids straddle a cap boundary, so this costs 1.05x the rows, not 5x.
    max_pct         INTEGER NOT NULL,
    year_min        INTEGER, year_max INTEGER,
    n_policies      INTEGER,
    best_win_rate   REAL,               -- max win rate over all policies
    best_win_combo  TEXT, best_win_props TEXT, best_win_avg_net REAL,
    best_net        REAL,               -- max average net return (per $1 protection)
    best_net_combo  TEXT, best_net_props TEXT, best_net_win_rate REAL,
    median_net      REAL,
    pct_positive    REAL,               -- share of policies with positive avg net
    -- Allocation-weighted PREMIUM RATE for each stored winning allocation:
    --     rate_sum = SUM(props_i/100 x prf_grid_rate.premium_rate_i)
    -- over that allocation's intervals, at this row's grid x use x coverage (newest rate
    -- year). Premium is protection x rate, and PRF protection per acre is
    -- CBV x coverage x productivity, so gross premium/acre = CBV x coverage x
    -- productivity x rate_sum. Stored here because prf_grid_rate (2.1M rows) is DROPPED
    -- from the shipped app DB — 2 REALs x 195k rows (~3 MB) buys the app the premium (and
    -- therefore the agent-commission) arithmetic without shipping the rate table.
    -- NULL when the grid has no rates for this use/coverage: never guessed.
    best_win_rate_sum REAL,
    best_net_rate_sum REAL,
    top_json        TEXT,               -- top-N policies by each metric, JSON
    source          TEXT, fetched_at TEXT,
    PRIMARY KEY (grid_id, intended_use, coverage_level, max_pct)
);

-- Which counties each PRF grid touches (a grid can span several counties and vice versa).
-- From the PrfWebApi GetCountiesAndStatesFromGridId call or the ADM grid-keyed records.
CREATE TABLE IF NOT EXISTS prf_grid_county (
    grid_id         INTEGER NOT NULL,
    state           TEXT NOT NULL,
    county_fips     TEXT NOT NULL,
    county_name     TEXT,
    source          TEXT,
    PRIMARY KEY (grid_id, county_fips)
);

-- PRF historical rainfall index values (grid grain): one row per grid x sample year x
-- two-month index interval. index_value is the index as a decimal "percent of normal"
-- (1.000 = normal rainfall; the support tool displays 0.798 as 79.8%). An indemnity
-- triggers when the final index falls below the coverage level (e.g. < 0.90).
-- Populated by src/prfdata.py from RMA's PRF support tool web API
-- (public-rma.fpac.usda.gov/apps/PrfWebApi, PrfExternalIndexes/GetIndexValues); data run
-- 1948 -> present. Interval codes are the ADM A00480 abbreviations (JAN-FEB ... NOV-DEC).
CREATE TABLE IF NOT EXISTS prf_grid_index (
    grid_id         INTEGER NOT NULL,
    year            INTEGER NOT NULL,
    interval_code   TEXT NOT NULL,     -- 'JAN-FEB' ... 'NOV-DEC' (11 overlapping intervals)
    index_value     REAL NOT NULL,     -- percent of normal, decimal (0.798 = 79.8%)
    source          TEXT,              -- e.g. prfwebapi
    fetched_at      TEXT,
    PRIMARY KEY (grid_id, year, interval_code)
);

-- PRF premium rates (grid grain): one row per grid x reinsurance year x intended use x
-- interval x coverage level. premium_rate is the unloaded rate applied to policy
-- protection (total premium = protection x rate). Populated by src/prfdata.py from the
-- PRF support tool web API (PrfExternalIntervalCodes/GetValidIntervalCodes); the same
-- numbers live in the ADM as A01130 Area Coverage Level (rc 05, keyed by grid) joined to
-- A01135 Area Rate via Area Rate ID (verified to match exactly for grid 27663).
CREATE TABLE IF NOT EXISTS prf_grid_rate (
    grid_id         INTEGER NOT NULL,
    year            INTEGER NOT NULL,  -- reinsurance year the rate applies to
    intended_use    TEXT NOT NULL,     -- Grazing | Haying | Haying-Irrigated
    interval_code   TEXT NOT NULL,     -- 'JAN-FEB' ... 'NOV-DEC'
    coverage_level  REAL NOT NULL,     -- 0.70 | 0.75 | 0.80 | 0.85 | 0.90
    premium_rate    REAL NOT NULL,
    source          TEXT,              -- e.g. prfwebapi_2026
    fetched_at      TEXT,
    PRIMARY KEY (grid_id, year, intended_use, interval_code, coverage_level)
);

-- Per-grid fingerprint of the prf_grid_index scoring window (2006-2024), so the monthly
-- update can re-score ONLY the grids whose index history actually moved. Written by
-- src/prfbulk.py after each bulk load / successful sweep; compared by prfbulk.changed_grids().
CREATE TABLE IF NOT EXISTS prf_index_hash (
    grid_id         INTEGER PRIMARY KEY,
    window_hash     TEXT,
    updated_at      TEXT
);

-- PRF premium subsidy schedule (national, plan 13): subsidy percent by coverage level.
-- From ADM A00070 Subsidy Percent (record category 04, plan 13, RY2026), cross-checked
-- against the support tool's SubsidyLevel. 0.65 is the CAT level (100% subsidized).
CREATE TABLE IF NOT EXISTS prf_subsidy (
    coverage_level  REAL PRIMARY KEY,
    subsidy_pct     REAL NOT NULL,
    source          TEXT
);

-- The shippable form of the two heaviest PRF tables, for the interval analyzer
-- (src/prfintervalpage.py): one row per grid holding the rainfall index for every
-- interval x year 2000-2025 plus that grid's premium rates, packed into bytes.
--
-- prf_grid_index (11.7M rows) and prf_grid_rate (2.1M rows) are ~1 GB and never ship. The
-- analyzer needs their CONTENT, though, not a summary of it: the user picks where a lookback
-- ends and how long it is (5/10/15/20 years) and the win rate / return per $1 / return per
-- acre must be recomputed, and the allocation optimizer re-run, over whatever window lands. So this table keeps every
-- interval x year cell and bounds only precision and span. src/prfintervals.py owns the
-- encoding and documents both bounds; the short version is that the index is one byte, at
-- 0.005 of normal wherever a payout is possible (worst-case payout error 0.0036 per $1 of
-- protection, at the 70% coverage level, and exactly zero error at or above the coverage
-- level where nothing pays anyway) and coarser above 0.90, and rates are exact.
--
-- Built by scripts/build_prf_interval_history.py from the local working catalog, the same
-- offline-reduce-then-ship pattern as nass_county_yield -> basis_risk_county.
CREATE TABLE IF NOT EXISTS prf_interval_history (
    grid_id     INTEGER PRIMARY KEY,
    year_min    INTEGER NOT NULL,   -- 2000 (the sweep's own window, 2006-2025, sits inside it)
    year_max    INTEGER NOT NULL,   -- 2025
    rate_year   INTEGER NOT NULL,   -- reinsurance year the packed rates are for
    index_blob  BLOB NOT NULL,      -- format byte + 11 intervals x 26 years, uint16 thousandths (pack_index)
    mean_blob   BLOB NOT NULL,      -- 11 intervals, uint16 at 1/1000: the TRUE full-span mean (unread by the page)
    rate_blob   BLOB NOT NULL,      -- 11 intervals x 5 coverages, uint16 at 1/10,000 (exact)
    use_mask    INTEGER NOT NULL,   -- which intervals each intended use is actually OFFERED on
    -- WHICH INDEX SERIES these bytes are, e.g. 'vi_ri_2027cy_ncei'. Stamped here rather than
    -- looked up, because prf_grid_index (where the vintage otherwise lives) is dropped from
    -- the shipped DB -- so on the deployed app this column is the only thing that knows, and
    -- the analyzer prints it. RMA's 2027 move from NOAA CPC to NOAA NCEI re-based the whole
    -- history, so "the rainfall index" is no longer one series. See src/prfbulk.
    index_source TEXT
);

-- FSA National Crop Table — Acres Per Animal Unit for GRAZING crop-use rows: the number of
-- acres of a given crop/type that must carry ONE animal unit for the grazing period, by
-- county. This is FSA's NAP/ARC-PLC stocking-rate figure, not an RMA table, and it is scoped
-- to the specific crop/type FSA prices for grazing in that county (annual forage crops like
-- wheat or oat pasture, and perennial GRASS types including NATIVE) — not a generic "PRF
-- ground" figure, because neither RMA nor FSA publishes county carrying capacity for
-- PRF-insured acreage as such. GRASS/NATIVE is the closest available analog to native
-- rangeland and is the widest-covered combination in the table (2,662 of 2,924 counties
-- with any grazing row, non-irrigated, FY2026), which is why the app defaults to it.
--
-- Two tables, same split as prf_grid_index/prf_grid_rate -> prf_opt_best: this one holds
-- the RAW planting-period grain (not collapsed: the source carries multiple rows for the
-- same county x crop x type x irrigation, one per FSA "Planting Period" code, and in
-- dry-country counties (observed on Arizona native range) they disagree by two orders of
-- magnitude — 1 to 160 acres/AU — because the figure depends on the state of the forage
-- when grazing begins; humid counties mostly carry a single period). It is a LOCAL-ONLY
-- working table: scripts/build_app_db.py drops it from the shipped copy (a text-heavy
-- ~125k rows would blow the 95 MB ship budget) in favor of fsa_grazing_county below, which
-- is what src/prfherdpage.py actually reads.
--
-- Source: FSA "National Crop Table FY {year}" (fsa.usda.gov/documents/national-crop-table-
-- fy-{year}), an .xlsx of ~750k rows across every FSA crop/use, not just grazing. Populated
-- by src/connectors/fsa_nct.py, gated behind --force like the other heavy connectors (the
-- file is ~100 MB).
CREATE TABLE IF NOT EXISTS fsa_grazing_capacity (
    year                   INTEGER NOT NULL,
    county_fips            TEXT NOT NULL,     -- 5-digit FIPS
    county_name_raw        TEXT NOT NULL,     -- FSA's own county name, as published (upper case)
    crop_name              TEXT NOT NULL,     -- e.g. GRASS, WHEAT, SORGHUM FORAGE
    crop_type_name         TEXT NOT NULL DEFAULT '',  -- e.g. NATIVE, COMMON BERMUDA
    irrigation_practice    TEXT NOT NULL,     -- Irrigated | Non-Irrigated
    planting_period        TEXT NOT NULL DEFAULT '',  -- FSA planting/grazing-period code
    acres_per_animal_unit  REAL NOT NULL,     -- acres needed to carry ONE animal unit
    grazing_days           INTEGER,           -- length of the grazing period assumed
    source                 TEXT,
    fetched_at             TEXT,
    PRIMARY KEY (year, county_fips, crop_name, crop_type_name, irrigation_practice,
                 planting_period)
);
CREATE INDEX IF NOT EXISTS idx_fsa_grazing_crop
    ON fsa_grazing_capacity(crop_name, crop_type_name);

-- Small lookup so the (usually long) crop name / type strings are stored ONCE per the ~207
-- distinct combinations rather than once per county row. "GRASS|NATIVE" (crop_key) is the
-- same string src/prfherdpage.py builds for the in-page crop selector. crop_id is an
-- ordinary integer surrogate assigned fresh by the connector on each refresh (nothing
-- outside this table and fsa_grazing_county's crop_id column persists it), which is what
-- lets fsa_grazing_county reference an INTEGER here instead of repeating ~20 bytes of TEXT
-- on every one of its ~70k rows.
CREATE TABLE IF NOT EXISTS fsa_crop_type (
    crop_id         INTEGER PRIMARY KEY,
    crop_key        TEXT NOT NULL UNIQUE,
    crop_name       TEXT NOT NULL,
    crop_type_name  TEXT NOT NULL DEFAULT ''
);

-- The SHIPPED rollup: one row per county x crop/type, collapsed from fsa_grazing_capacity's
-- planting-period grain, NON-IRRIGATED ONLY. acres_per_animal_unit is the MAX across a
-- cell's planting periods — the conservative figure for a herd grazed there continuously
-- through its leanest period — with min_acres_per_animal_unit and n_periods kept alongside
-- so the app can still show the true spread without shipping every period. This is what
-- src/prfherdpage.py queries; fsa_grazing_capacity itself never ships (see above).
--
-- Non-irrigated only, not a size cut dressed up as a modeling choice: PRF's own "Grazing"
-- intended use has no irrigated/non-irrigated split (src/connectors/prf_adm.py maps
-- Grazing's ADM code straight to Non-Irrigated; only Haying carries the distinction), so an
-- Irrigated grazing figure was never a PRF-relevant number in the first place. Dropping it
-- also cut this table from 90,242 to 69,506 rows -- one of two changes (with crop_id above)
-- that got the shipped DB back under the 95 MB budget.
CREATE TABLE IF NOT EXISTS fsa_grazing_county (
    year                        INTEGER NOT NULL,
    county_fips                 TEXT NOT NULL,
    crop_id                     INTEGER NOT NULL,   -- -> fsa_crop_type.crop_id
    acres_per_animal_unit       REAL NOT NULL,       -- MAX across the cell's planting periods
    min_acres_per_animal_unit   REAL NOT NULL,
    n_periods                   INTEGER NOT NULL,
    grazing_days                INTEGER,
    PRIMARY KEY (year, county_fips, crop_id)
);

-- ---------------------------------------------------------------------------
-- DRP (Dairy Revenue Protection, plan code 83, ADM commodity 0830 "Milk").
--
-- GRAIN, verified against the ADM and NOT what a county-level product looks like: DRP is
-- offered STATEWIDE. Every plan-83 row in ADM A00030 Insurance Offer carries County Code
-- '998' (all counties), so the offer grain is state x quarter x pricing option and there is
-- deliberately no drp_county table — drp_state carries availability instead. RY2026 has
-- exactly 800 plan-83 offers = 50 states x 8 quarters x 2 pricing options, and the fact
-- sheet agrees ("Dairy-RP is available in all counties in all 50 states").
--
-- THERE IS NO PUBLISHED DRP PREMIUM RATE TABLE, so there is no drp_rate table. DRP premium
-- is not a rate lookup: RMA's M13 exhibit P18-1 (Plan 83, Premium Calculation) specifies a
-- 5,000-iteration Monte Carlo over lognormal price draws, i.e.
--     SimulatedLossAverage = ROUND(MAX(SUM(SimulatedLoss[seq]) / 5000.00,
--                                      0.02 * DeclaredCoveredMilkProduction / 100.00), 2)
--     TotalPremiumAmount   = ROUND(ROUND(SimulatedLossAverage * DeclaredShare
--                                        * ProtectionFactor, 0) * LoadingFactor, 0)
-- (Units: every revenue formula divides DeclaredCoveredMilkProduction by 100 against a
-- $/cwt price, and drp_milk_yield is pounds per cow — so declared production is POUNDS.
-- A worked end-to-end run of the above on RY2026 WI 2026Q4 Class pricing at 90% coverage,
-- PF 1.00, 50/50 Class III/IV came out at $0.227/cwt producer premium: the right order of
-- magnitude for real DRP, which is the plausibility check this schema is built to support.)
-- Every INPUT to that simulation is public, and the tables below are exactly those inputs:
-- drp_daily_price (expected prices + sigmas + loading factor), drp_draw (RMA's fixed
-- uniform draws, which AIPs must use verbatim), drp_milk_yield, drp_fmmo_factor,
-- drp_subsidy. So premium is reproducible, just not lookup-able.
--
-- Source files, all bulk zips under
--   https://pubfs-rma.fpac.usda.gov/pub/References/adm_livestock/{reinsurance_year}/
--   {RY}_A00831_ADMDrpDraw_Quarterly_{YYYYMMDD}.zip        -> drp_draw
--   {RY}_A00832_ADMDrpMilkYield_Quarterly_{YYYYMMDD}.zip   -> drp_milk_yield
--   {RY}_A00833_ADMDrpDailyPrice_Daily_{YYYYMMDD}.zip      -> drp_daily_price
--   {RY}_A00834_ADMDrpActualPrice_Quarterly_{YYYYMMDD}.zip -> drp_actual_price
--   {RY}_A00835_ADMDrpFmmoPricingFactor_Yearly_{YYYYMMDD}.zip -> drp_fmmo_factor
-- plus the crop ADM (A00030 offers, A00070 subsidy, A00510/A00540/A00480/A00520 code
-- tables) for drp_offer / drp_subsidy / drp_state. Populated by src/drpdata.py. Data runs
-- from RY2019 (DRP's first year) forward. The reinsurance year rolls on July 1, exactly as
-- lrp_signal.py already handles for LRP.

-- The DRP offer dimension: one row per ADM Insurance Offer ID, which is the key every
-- daily-price row joins on. From ADM A00030 filtered to Insurance Plan Code 83, decorated
-- with the ADM code tables. quarter_year/quarter_start/quarter_end resolve the ADM's
-- relative interval names ("Apr - Jun/Yr3 - Qtr2") into absolute calendar dates: Yr1 =
-- reinsurance_year - 1, Yr2 = reinsurance_year, Yr3 = reinsurance_year + 1 (verified — for
-- RY2026, interval 104 "Jul - Sep/Yr2 - Qtr3" was the nearest quarter offered on the
-- 2026-04-01 sales date, i.e. Jul-Sep 2026).
CREATE TABLE IF NOT EXISTS drp_offer (
    reinsurance_year INTEGER NOT NULL,
    offer_id        INTEGER NOT NULL,  -- ADM Insurance Offer ID
    commodity_code  TEXT,              -- always '0830' (Milk)
    plan_code       TEXT,              -- always '83'
    state_code      TEXT NOT NULL,     -- 2-digit FIPS state
    state_abbrev    TEXT,              -- 2-letter USPS
    county_code     TEXT,              -- always '998' = statewide (kept for provenance)
    type_code       TEXT NOT NULL,     -- 831 = Class Price Option, 832 = Component Price Option
    pricing_option  TEXT,              -- 'Class' | 'Component' (decoded from type_code)
    practice_code   TEXT NOT NULL,     -- 801..808, the quarterly coverage endorsement
    interval_code   TEXT,              -- 101..108, 1:1 with practice_code (practice = interval + 700)
    interval_name   TEXT,              -- e.g. 'Jul - Sep/Yr2 - Qtr3'
    quarter_year    INTEGER,           -- absolute calendar year of the insured quarter
    quarter         INTEGER,           -- 1..4
    quarter_start   TEXT,              -- ISO date, first day of the insured quarter
    quarter_end     TEXT,              -- ISO date, last day of the insured quarter
    deleted_date    TEXT,
    source          TEXT,              -- e.g. adm_2026_ytd
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_drp_offer_lookup
    ON drp_offer (reinsurance_year, state_code, quarter_year, quarter, pricing_option);

-- Daily expected prices + volatilities: the sales-date-grain price discovery an optimizer
-- scores against, and the parameters of the lognormal price simulation. From A00833.
-- One row per sales date x offer, so ~450 rows/day (50 states x 5 nearby quarters x 1-2
-- pricing options; the far-out quarters offer Class pricing only, because the butter/cheese/
-- whey/NFDM futures strips do not extend far enough for Component pricing).
-- Class-priced rows populate class3/class4 and leave the component columns NULL, and
-- Component-priced rows do the reverse — that is RMA's encoding, not missing data.
-- The *_restricted columns are RMA's constraint on the producer's declared weighting factor:
-- when non-NULL the declared factor MUST equal it (1 = only Class III / only protein+other
-- solids is published; 0 = only Class IV / only nonfat solids).
-- The DRP sales window, verified 2026-08-07 against RMA primary sources:
--   POST  RMA validates and publishes each day's coverage prices and rates "by 4:30pm CST"
--         (DRP fact sheet, June 2025); if they "are not available on the RMA website by
--         4:30pm, then Dairy-RP will not be offered for sale for the insurance period."
--         The Basic Provisions fix no clock time for posting — only this 4:30 PM CT deadline
--         is documented, so treat 4:30 PM CT as a not-later-than, not a typical arrival.
--   CLOSE 9:00 AM Central Time. 26-DRP Basic Provisions, s.1 Definitions, "Sales period":
--         "The period of time that begins when a daily set of coverage prices and rates are
--         posted on RMA's website and ends at 9:00 AM Central Time the earlier of Sunday or
--         the following business day in which you can purchase quarterly endorsements."
--         Same wording in DRP handbook FCIC-20400U p.6 s.23.B(2)-(3). Unchanged since
--         19-DRP (RY2019, DRP's first year); the only amendment was June 2020, which added
--         the "earlier of Sunday" weekend clause. So this holds for every RY in this table.
-- CAUTION: 9:00 AM CT is DRP's own close and is CORRECT. It is NOT the same as LRP's 8:25 AM
-- CT close (27-LRP Basic Provisions, "Sales period") that lrp_signal.py encodes as 505 min.
-- The two programs genuinely differ — do not "harmonize" this to 8:25.
CREATE TABLE IF NOT EXISTS drp_daily_price (
    reinsurance_year INTEGER NOT NULL,
    sales_date      TEXT NOT NULL,     -- ISO date; see the sales-window note above
    offer_id        INTEGER NOT NULL,  -- -> drp_offer
    daily_price_id  INTEGER,           -- ADM Drp Daily Price ID
    loading_factor  REAL,              -- multiplies the simulated pure premium
    -- Class pricing: monthly futures-derived expected prices and their lognormal sigmas.
    m1_class3 REAL, m2_class3 REAL, m3_class3 REAL,
    m1_class4 REAL, m2_class4 REAL, m3_class4 REAL,
    m1_class3_sigma REAL, m2_class3_sigma REAL, m3_class3_sigma REAL,
    m1_class4_sigma REAL, m2_class4_sigma REAL, m3_class4_sigma REAL,
    -- Component pricing: the four manufactured-product futures the FMMO formulas run on.
    m1_butter REAL, m2_butter REAL, m3_butter REAL,
    m1_cheese REAL, m2_cheese REAL, m3_cheese REAL,
    m1_dry_whey REAL, m2_dry_whey REAL, m3_dry_whey REAL,
    m1_nfdm REAL, m2_nfdm REAL, m3_nfdm REAL,
    m1_butter_sigma REAL, m2_butter_sigma REAL, m3_butter_sigma REAL,
    m1_cheese_sigma REAL, m2_cheese_sigma REAL, m3_cheese_sigma REAL,
    m1_dry_whey_sigma REAL, m2_dry_whey_sigma REAL, m3_dry_whey_sigma REAL,
    m1_nfdm_sigma REAL, m2_nfdm_sigma REAL, m3_nfdm_sigma REAL,
    -- Quarter-level expected values: what the revenue guarantee is built from.
    expected_class3 REAL, expected_class4 REAL,
    expected_butterfat REAL, expected_protein REAL,
    expected_other_solids REAL, expected_nonfat_solids REAL,
    component_weight_restricted REAL,  -- NULL = producer may choose; else must equal this
    class_weight_restricted     REAL,
    milk_yield_id   INTEGER,           -- -> drp_milk_yield
    actual_price_id INTEGER,           -- -> drp_actual_price (the settlement record for this quarter)
    fmmo_factor_id  INTEGER,           -- -> drp_fmmo_factor
    released_date   TEXT,
    filing_date     TEXT,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, sales_date, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_drp_daily_price_offer
    ON drp_daily_price (reinsurance_year, offer_id, sales_date);

-- Settled (actual) quarterly prices — the realized side of the revenue equation, from the
-- AMS monthly announced prices. From A00834.
--
-- The key is (reinsurance_year, actual_price_id) and NOT the quarter, because one calendar
-- quarter can carry TWO records within a single RY. Verified in RY2025: ids 49-56 were
-- published 2024-06-24 under FMMO pricing-factor set 7 and ids 57-62 were published
-- 2025-01-19 under set 8 (the June 2025 FMMO make-allowance change). Ids 55 and 61 both
-- settle the Apr-Jun 2026 quarter with IDENTICAL butter/cheese/whey/NFDM prices but
-- DIFFERENT derived butterfat/protein/other-solids, because the FMMO formulas changed.
-- So never key actual prices by quarter: reach them through drp_daily_price.actual_price_id
-- for the sales date the endorsement was actually bought on. Columns are NULL until the
-- quarter has ended and AMS has announced.
CREATE TABLE IF NOT EXISTS drp_actual_price (
    reinsurance_year INTEGER NOT NULL,
    actual_price_id INTEGER NOT NULL,
    m1_butter REAL, m2_butter REAL, m3_butter REAL,
    m1_cheese REAL, m2_cheese REAL, m3_cheese REAL,
    m1_dry_whey REAL, m2_dry_whey REAL, m3_dry_whey REAL,
    m1_nfdm REAL, m2_nfdm REAL, m3_nfdm REAL,
    actual_class3 REAL, actual_class4 REAL,
    actual_butterfat REAL, actual_protein REAL,
    actual_other_solids REAL, actual_nonfat_solids REAL,
    settled          INTEGER DEFAULT 0,  -- 1 once actual_class3 (or actual_butterfat) is populated
    released_date   TEXT,
    filing_date     TEXT,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, actual_price_id)
);

-- State milk yield (lb/cow/quarter) — the SECOND stochastic term in DRP: revenue is
-- price x yield, so a yield shortfall in the producer's pooled region can trigger an
-- indemnity even when prices hold. From A00832. The record itself carries only the state;
-- the quarter comes from whichever drp_daily_price row points at it. actual_yield stays
-- NULL until the quarter of coverage has passed (RMA's own note in the ADM layout).
-- expected_yield_sd is quarterly pounds per cow; see the DRP Commodity Exchange Endorsement
-- for which states are POOLED (a value is published for every state regardless).
--
-- KNOWN GAP IN RMA'S OWN DATA (verified, not a loader defect): RY2025 daily-price rows for
-- the Jul-Sep 2026 quarter on the 22 sales dates 2025-03-17..2025-04-21 point at milk-yield
-- ids 10031-10080 (one per state) that RMA never published — those ids appear in none of the
-- 101 A00832 files across every reinsurance year. That is 1,100 of 671,000 daily-price rows
-- (0.164%). Joins to this table are therefore LEFT joins and expected_yield can be NULL; the
-- gap is left unfilled rather than interpolated. A consumer that needs a yield for those rows
-- should fall back to the adjacent generation for the same state and quarter, explicitly.
CREATE TABLE IF NOT EXISTS drp_milk_yield (
    reinsurance_year INTEGER NOT NULL,
    milk_yield_id   INTEGER NOT NULL,
    state_code      TEXT NOT NULL,
    state_abbrev    TEXT,
    expected_yield  REAL,              -- expected milk production per cow
    actual_yield    REAL,              -- NULL until the covered quarter has passed
    expected_yield_sd REAL,
    released_date   TEXT,
    filing_date     TEXT,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, milk_yield_id)
);

-- Federal Milk Marketing Order pricing formulas (manufacturing yields + make allowances)
-- that convert butter/cheese/dry whey/NFDM prices into butterfat/protein/other-solids
-- component prices and into Class III/IV. From A00835. One or two rows per RY — the set
-- changes when FMMO rules change (see the drp_actual_price note).
CREATE TABLE IF NOT EXISTS drp_fmmo_factor (
    reinsurance_year INTEGER NOT NULL,
    fmmo_factor_id  INTEGER NOT NULL,
    butter_mfg_yield REAL,
    nfdm_mfg_yield   REAL,
    dry_whey_mfg_yield REAL,
    cheese_mfg_yield_casein REAL,
    cheese_mfg_yield_butterfat REAL,
    butterfat_retention_rate REAL,
    butterfat_to_protein_ratio REAL,
    butter_make_allowance REAL,
    nfdm_make_allowance REAL,
    dry_whey_make_allowance REAL,
    cheese_make_allowance REAL,
    released_date   TEXT,
    filing_date     TEXT,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, fmmo_factor_id)
);

-- RMA's fixed uniform (0,1) random draws — 5,000 sequences per state x quarter. AIPs must
-- simulate with THESE numbers, not their own, which is why RMA publishes them; they are
-- what makes DRP premium exactly reproducible. From A00831, keyed by the same milk-yield id
-- the daily price row points at. HEAVY: ~1.25M rows and ~226 MB of text per quarterly file
-- (250 milk-yield ids x 5,000 draws), and the draws are genuinely distinct per state and
-- quarter (checked — no redundancy to collapse). Loading is therefore opt-in behind
-- `python -m src.drpdata --draws`; everything else works without it.
CREATE TABLE IF NOT EXISTS drp_draw (
    reinsurance_year INTEGER NOT NULL,
    milk_yield_id   INTEGER NOT NULL,  -- -> drp_milk_yield (encodes state x quarter)
    draw_number     INTEGER NOT NULL,  -- 1001..6000 (5,000 sequences)
    state_code      TEXT,
    m1_class3 REAL, m2_class3 REAL, m3_class3 REAL,
    m1_class4 REAL, m2_class4 REAL, m3_class4 REAL,
    m1_butter REAL, m2_butter REAL, m3_butter REAL,
    m1_cheese REAL, m2_cheese REAL, m3_cheese REAL,
    m1_dry_whey REAL, m2_dry_whey REAL, m3_dry_whey REAL,
    m1_nfdm REAL, m2_nfdm REAL, m3_nfdm REAL,
    yield_draw REAL,
    source          TEXT,
    PRIMARY KEY (reinsurance_year, milk_yield_id, draw_number)
);

-- DRP premium subsidy by coverage level (plan 83). From ADM A00070 Subsidy Percent, record
-- category 04, which is the source P18-1 itself names ("Subsidy Percent A00070 field 15 —
-- Edit with ADM Subsidy Percent"). RY2026 values 0.80->0.550, 0.85->0.490, 0.90->0.440,
-- 0.95->0.440 match the DRP fact sheet's "Coverage Level % 80 85 90 95 / Premium Subsidy %
-- 55 49 44 44" exactly. NOTE: unlike most plans, DRP has no CAT level.
-- Beginning/Veteran Farmer & Rancher adds a flat 10% of total premium on top (P18-1 §9);
-- producer premium is floored at $1.
--
-- THE LEVEL SET IS NOT CONSTANT ACROSS YEARS. RY2019 — DRP's first year — filed SIX levels
-- (0.70->0.590, 0.75->0.550, 0.80->0.550, 0.85->0.490, 0.90->0.440, 0.95->0.440); from
-- RY2020 on it is the four levels above. So a backtest that spans 2019 must read the levels
-- from this table per year, not from a hardcoded 80/85/90/95. (A00070's own layout drifted
-- too: 18 fields in RY2019 vs 19 from RY2020, when Range Low/High Count became Range Type
-- Code + Range Low/High Value — which is why the loader parses by column NAME.)
CREATE TABLE IF NOT EXISTS drp_subsidy (
    reinsurance_year INTEGER NOT NULL,
    coverage_level  REAL NOT NULL,     -- 0.80 | 0.85 | 0.90 | 0.95
    coverage_type_code TEXT,           -- 'A' (additive/buy-up); DRP publishes no CAT row
    subsidy_pct     REAL NOT NULL,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, coverage_level)
);

-- DRP availability, at the grain the product is actually sold: state, not county (see the
-- header note — every plan-83 offer carries County Code 998). Derived from drp_offer, so
-- it is a materialized rollup rather than a separate fetch: n_quarters/n_pricing_options
-- record what was actually filed for that state in that reinsurance year.
CREATE TABLE IF NOT EXISTS drp_state (
    reinsurance_year INTEGER NOT NULL,
    state_code      TEXT NOT NULL,     -- 2-digit FIPS
    state_abbrev    TEXT,
    state_name      TEXT,
    n_quarters      INTEGER,           -- distinct quarterly endorsements filed (expect 8)
    n_pricing_options INTEGER,         -- expect 2 (Class + Component)
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, state_code)
);

-- DRP optimizer results (STATE grain, because that is the grain DRP is sold at — see the
-- header note; there is no county or grid level below this and no drp_state_county table
-- to roll up from). Written by src/drpopt.py, read by src/drppage.py. The analogue of
-- prf_opt_best, and normalized the same way: every metric is PER $1 OF LIABILITY.
--
-- THE SEARCH SPACE IS 84 RISK SHAPES PER PRICING OPTION, not the 1,848 declarations
-- drpdata.declaration_space counts. The protection factor (and declared share, and
-- declared production) appears in M13 P18-1's TotalPremiumAmount AND Liability and in
-- P28-1's indemnity, so it scales cost and payout identically: it can change how many
-- dollars are at stake and cannot change a win rate or a return per dollar. What is left
-- is 4 coverage levels x 21 weighting factors. Each row here fixes the coverage level and
-- stores the best of the 21 weighting factors within it.
--
-- One row per (state, pricing option, quarter, coverage level). quarter is 1..4, the
-- calendar quarter of the insured period, plus 0 = every quarter pooled (the rollup the
-- map shades by default). Observations are SETTLED quarters, one per calendar quarter at
-- one sales date; 2019Q1..2026Q2 gives up to 30 per state per pricing option.
--
-- There is no premium-rate column because there is no DRP rate table: premium is a
-- 5,000-iteration Monte Carlo (P18-1) over drp_daily_price + drp_draw, both of which are
-- DROPPED from the shipped app DB. best_net_prem / best_win_prem carry the simulated
-- PRODUCER premium per $1 of liability, and best_net_liability_cwt carries the dollar
-- liability on one hundredweight — together they are the only premium information that
-- survives dropping the inputs, exactly as prf_opt_best.best_net_rate_sum is for PRF.
CREATE TABLE IF NOT EXISTS drp_opt_best (
    state_code      TEXT NOT NULL,     -- 2-digit FIPS
    state_abbrev    TEXT,
    pricing_option  TEXT NOT NULL,     -- 'Class' | 'Component'
    quarter         INTEGER NOT NULL,  -- 1..4; 0 = all quarters pooled
    coverage_level  REAL NOT NULL,
    quarter_min     TEXT,              -- e.g. '2019Q1' — first settled quarter scored
    quarter_max     TEXT,              -- e.g. '2026Q2'
    n_obs           INTEGER,           -- settled quarters each shape was scored on
    n_shapes        INTEGER,           -- weighting factors actually scored (<= 21)
    n_pinned        INTEGER,           -- observations where RMA pinned the weighting factor
    best_win_rate   REAL,              -- max share of quarters with a positive net
    best_win_weight REAL, best_win_net REAL, best_win_prem REAL,
    best_net        REAL,              -- max mean net return PER $1 OF LIABILITY
    best_net_weight REAL, best_net_win_rate REAL, best_net_prem REAL,
    best_net_liability_cwt REAL,       -- mean $ liability on 100 lb, best-net shape
    median_net      REAL,
    pct_positive    REAL,              -- share of shapes with a positive mean net
    premium_draw_ry INTEGER,           -- reinsurance year of the drp_draw set simulated with
    top_json        TEXT,              -- top-N shapes by each metric + the pins seen, JSON
    source          TEXT, fetched_at TEXT,
    PRIMARY KEY (state_code, pricing_option, quarter, coverage_level)
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER,
    url             TEXT NOT NULL,
    local_path      TEXT,
    sha256          TEXT,
    content_type    TEXT,
    fetched_at      TEXT,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

-- SERFF filing-level records (regulatory grain: one row per state filing, not per product —
-- an AIP re-files rates/forms yearly, so filings vastly outnumber products and get their own
-- table instead of swamping `products`). Populated by the serff connector from the JSON payloads
-- that the Playwright extractor (src/connectors/serff_browser.py) writes to data/cache/serff/.
CREATE TABLE IF NOT EXISTS serff_filings (
    serff_tracking_number TEXT NOT NULL,
    state           TEXT NOT NULL,
    filing_id       TEXT,              -- portal-internal id (filingSummary.xhtml?filingId=...)
    company_name    TEXT,              -- as shown on the portal (name variants differ from aips)
    aip_code        TEXT,              -- mapped to aips when the company name matches
    naic_company_code TEXT,
    product_name    TEXT,
    toi             TEXT,
    sub_toi         TEXT,              -- 02.1000/02.1001/02.1002 split lives here
    filing_type     TEXT,              -- Form | Rate/Rule | Form/Rate/Rule
    filing_status   TEXT,
    disposition_status TEXT,
    submission_date TEXT,              -- ISO when available (details phase); else NULL
    disposition_date TEXT,
    filing_url      TEXT,
    fetched_at      TEXT,
    raw             TEXT,
    PRIMARY KEY (serff_tracking_number, state)
);

-- HTTP cache so refresh only re-downloads changed URLs (freshness gate; --force bypasses).
CREATE TABLE IF NOT EXISTS url_cache (
    url             TEXT PRIMARY KEY,
    local_path      TEXT,
    etag            TEXT,
    last_modified   TEXT,
    sha256          TEXT,
    http_status     INTEGER,
    fetched_at      TEXT
);

-- Provenance: which connector ran, when, how much it returned, and any error.
CREATE TABLE IF NOT EXISTS fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    target          TEXT,              -- e.g. state code or AIP name
    status          TEXT,              -- ok | error | skipped
    http_status     INTEGER,
    rows            INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    message         TEXT
);

-- ===========================================================================
-- BASIS RISK (area-triggered endorsements: SCO, ECO, MCO, STAX)
-- ===========================================================================

-- NASS Quick Stats yield history, LOCAL ONLY. This is the raw input to the basis-risk
-- estimator and is the exact analogue of prf_grid_index / drp_daily_price: ~1M rows the
-- app never queries. build_app_db.py MUST drop it (see docs/basis_risk.md).
--
-- Four aggregation levels are loaded, not one. COUNTY is the series SCO/ECO settle on;
-- AGRICULTURAL DISTRICT / STATE / NATIONAL are there because the basis-risk model needs
-- one parameter it cannot read off a county series (the farm/county variance ratio), and
-- the way yield variance falls as you aggregate county -> district -> state -> national
-- is the only public, data-driven handle on it (src/basisrisk.py: aggregation_scaling).
--
-- Grain is NASS's own: a yield series is identified by crop x class x production practice
-- x unit, and all of those matter. Wheat has no single national series (WINTER vs SPRING
-- vs DURUM); corn splits GRAIN from SILAGE; the Plains split NON-IRRIGATED into CONTINUOUS
-- CROP vs FOLLOWING SUMMER FALLOW, which is an RMA practice split too. Nothing is
-- collapsed here -- the choice of which series represents a county is made downstream and
-- recorded in basis_risk_county.class_used / practice_used.
--
-- unit: 'BU / ACRE' is yield per HARVESTED acre; 'BU / NET PLANTED ACRE' includes
-- abandonment and therefore carries the deeper downside tail. Both load; the harvested
-- series is the default because it has far more county-years, and the planted series is
-- the robustness check.
-- AREA HARVESTED rides in the same table as YIELD (the `stat` column separates them) because
-- the aggregation-scaling calibration needs the ACRES behind each reporting unit, not a proxy
-- for it: the farm/county variance ratio the model needs is a ratio of AREAS, and a county's
-- planted acreage varies by more than an order of magnitude across the country.
CREATE TABLE IF NOT EXISTS nass_county_yield (
    crop            TEXT NOT NULL,     -- canonical catalog crop (Corn / Soybeans / Wheat)
    stat            TEXT NOT NULL,     -- YIELD | AREA HARVESTED
    class_desc      TEXT NOT NULL,     -- NASS CLASS_DESC (ALL CLASSES, WINTER, SPRING ...)
    practice        TEXT NOT NULL,     -- NASS PRODN_PRACTICE_DESC
    unit            TEXT NOT NULL,     -- BU / ACRE | BU / NET PLANTED ACRE | ACRES
    agg_level       TEXT NOT NULL,     -- COUNTY | DISTRICT | STATE | NATIONAL
    loc_key         TEXT NOT NULL,     -- COUNTY: 5-digit FIPS; DISTRICT: 'ss-dd'; STATE: 'ss'; NATIONAL: 'US'
    state           TEXT,              -- 2-letter USPS (NULL at NATIONAL)
    county_fips     TEXT,              -- 5-digit FIPS (COUNTY rows only)
    asd_code        TEXT,              -- NASS agricultural statistics district
    county_name     TEXT,
    year            INTEGER NOT NULL,
    value           REAL NOT NULL,     -- bu/acre for YIELD, acres for AREA HARVESTED
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (crop, stat, class_desc, practice, unit, agg_level, loc_key, year)
);
CREATE INDEX IF NOT EXISTS idx_nass_yield_county
    ON nass_county_yield (crop, stat, agg_level, loc_key, year);

-- Precomputed per-county basis risk. This is the table that SHIPS -- the same call as
-- prf_opt_best: the raw history (nass_county_yield, ~1M rows) is the estimator's INPUT and
-- stays local, while this compact result survives dropping it.
--
-- ONE ROW = one (crop, county, band, plan type, farm coverage level). Every probability is
-- an ANNUAL frequency for a TYPICAL farm in that county -- never for any actual farm. We
-- hold no farm-level yield data and cannot get it (it is private), so the farm side of
-- every number here is MODELLED from the county series plus one imported parameter, rho
-- (farm-county yield correlation). Read miss_rate together with rho_ref, the rho_lo/rho_hi
-- sensitivity, the bootstrap CI and `grade`, or it will be over-read. The farm-specific
-- answer comes from src/basisrisk.py's farm calculator, which uses the producer's own APH.
--
-- MEASURED columns (county_cv, trend_*, p_county_below_trigger, corr_national, n_years)
-- come straight from the NASS series. MODELLED columns (everything from miss_rate on) come
-- from the simulation described in docs/basis_risk.md.
CREATE TABLE IF NOT EXISTS basis_risk_county (
    crop            TEXT NOT NULL,
    county_fips     TEXT NOT NULL,
    state           TEXT,
    county_name     TEXT,
    band            TEXT NOT NULL,     -- ECO95 | ECO90 | SCO86
    plan_type       TEXT NOT NULL,     -- RP (revenue trigger) | YP (yield trigger)
    coverage_level  REAL NOT NULL,     -- the FARM's own MPCI coverage level = its deductible
    -- ---- MEASURED: the county series itself -------------------------------
    n_years         INTEGER,           -- usable detrended years; drives `grade`
    year_min        INTEGER,
    year_max        INTEGER,
    class_used      TEXT,              -- which NASS class/practice series represented this county
    practice_used   TEXT,
    detrend_method  TEXT,              -- ols | theilsen
    trend_bu_per_year REAL,
    trend_pct_per_year REAL,
    trend_r2        REAL,
    mean_yield      REAL,
    county_cv       REAL,              -- SD of the DETRENDED yield ratio (actual / trend)
    county_skew     REAL,
    -- Model-free, and the only column here with no simulation behind it at all: the share of
    -- the county's own observed years whose detrended YIELD ratio fell below the trigger. For
    -- a YP band that is exactly how often it would have fired. For an RP band it is the
    -- yield-only floor — the real RP index also moves with the harvest price, so RP fires
    -- somewhat more often than this. Kept deliberately raw so there is one number on every row
    -- a reader can check against the county's history by hand.
    p_county_below_trigger REAL,
    corr_national   REAL,              -- county vs national detrended residual (systemic share)
    -- ---- MODELLED: basis risk at the reference correlation ----------------
    rho_ref         REAL,              -- farm-county yield correlation assumed
    miss_rate       REAL,              -- HEADLINE: P(band pays NOTHING | farm loss beyond its deductible)
    p_hard_miss     REAL,              -- joint annual frequency of that event
    p_farm_loss_given_no_pay REAL,     -- P(farm loss | county index above trigger)
    deep_miss_rate  REAL,              -- P(band pays nothing | farm loss 10+ points beyond deductible)
    windfall_rate   REAL,              -- P(band pays | farm had NO loss) -- the transfer, not insurance
    uncovered_share REAL,              -- share of the farm's in-band loss dollars left uncovered
    payout_corr     REAL,              -- corr(farm in-band loss, band payment)
    -- ---- sensitivity to rho (the one imported parameter) ------------------
    rho_lo          REAL, miss_rate_rho_lo REAL,
    rho_hi          REAL, miss_rate_rho_hi REAL,
    -- ---- uncertainty from the length of the county series -----------------
    miss_rate_ci_lo REAL, miss_rate_ci_hi REAL,   -- bootstrap over years, at rho_ref
    grade           TEXT,              -- A (>=30 yrs) | B (20-29) | C (12-19); <12 not written
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (crop, county_fips, band, plan_type, coverage_level)
);
CREATE INDEX IF NOT EXISTS idx_basis_risk_state ON basis_risk_county (state, crop, band);

-- LGM (Livestock Gross Margin, plan 82) — the MARGIN leg, alongside LRP (price) and DRP
-- (revenue). Populated by src/lgm.py. See that module's docstring for the citations.
--
-- THE KEY IS THE DEDUCTIBLE, NOT A COVERAGE LEVEL. This is the one plan in the catalog
-- whose subsidy is not a function of a coverage level, and it is not a quirk of how we
-- loaded it: ADM A00070 Subsidy Percent carries plan 82 under record category 05 keyed on
-- `Deductible Amount`, with `Coverage Level Percent` blank on every row — while plan 81
-- (LRP) uses category 08 keyed on Range Low/High Value and plan 83 (DRP) uses category 04
-- keyed on Coverage Level Percent. A $0 deductible is NOT unsubsidised; it draws 18%, the
-- bottom rung. The zero-subsidy case is UNPOOLED coverage (target marketings in only one
-- month), which zeroes the subsidy at every deductible and is a property of the marketing
-- plan, so it is recorded on lgm_deductible_curve rather than here.
-- RY2026 and RY2027 ladders are identical value-for-value; the One Big Beautiful Bill Act
-- changes RMA cites for 2027 land on the beginning-farmer add-on, not on this table.
CREATE TABLE IF NOT EXISTS lgm_subsidy (
    reinsurance_year INTEGER NOT NULL,
    commodity_code  TEXT NOT NULL,     -- 0803 Cattle | 0815 Swine | 0847 Dairy Cattle
    commodity_name  TEXT,
    deductible      REAL NOT NULL,     -- $/head (cattle, swine) or $/cwt of milk (dairy)
    subsidy_pct     REAL NOT NULL,     -- 0.18 .. 0.50, POOLED coverage only
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, commodity_code, deductible)
);

-- One LGM margin panel: expected gross margins and the published draw set for a
-- (commodity, type, state) on one sales effective date, summarised. The full draw matrix
-- is not stored — it is ~31 MB per file and is re-read from data/cache/lgm/ on demand.
--
-- n_draws IS 500, NOT 5,000. RMA's own "LGM-Cattle Premium Calculation" instructions
-- (July 2020) specify i = 1..5,000 and divide by 5,000, but the published ADM member
-- A00610 LgmDraw carries Margin Draw Number 1..500 for every commodity in both RY2026 and
-- RY2027. src/lgm.py divides by the count it was handed and records it here so a premium
-- computed off the public file can be read as the 500-draw estimate it is.
--
-- The ration columns are RMA's declared feed/animal quantities per unit of exposure, which
-- is what LGM settles on instead of the operation's real feed bill. For cattle they come
-- off the A00600 row itself (Corn / Live Cattle / Feeder Cattle Equivalent Default Value)
-- and agree exactly with the handbook constants; for swine and dairy the ADM publishes the
-- margin already netted of feed, so they come from the handbook.
CREATE TABLE IF NOT EXISTS lgm_margin (
    reinsurance_year INTEGER NOT NULL,
    commodity_code  TEXT NOT NULL,
    commodity_name  TEXT,
    type_code       TEXT NOT NULL,     -- 807 Calf Finishing | 808 Yearling Finishing | ...
    type_name       TEXT,
    state_code      TEXT NOT NULL,     -- 2-digit FIPS; county is always 998 (statewide)
    sales_effective_date TEXT NOT NULL,
    months          TEXT,              -- comma-separated insured months actually populated
    expected_margin_total REAL,        -- sum over months, per unit of exposure
    expected_margin_mean  REAL,
    n_draws         INTEGER,
    draw_total_sd   REAL,              -- sd of the summed simulated margin, per unit
    liability_price REAL,
    ration_corn_bu  REAL,
    ration_soybean_meal_ton REAL,
    ration_feeder_cwt REAL,
    ration_output_cwt REAL,
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, commodity_code, type_code, state_code,
                 sales_effective_date)
);

-- The deductible curve: RMA's Monte Carlo premium re-run at every filed deductible with the
-- marketing plan held fixed, so the row-to-row difference is the deductible alone.
--
-- WHY THE OPTIMUM IS INTERIOR. Raising the deductible raises the subsidy RATE but shrinks
-- the premium BASE, and the rate stops rising once the ladder caps at 0.50 while the base
-- keeps falling. net_expected_gain = total_premium * (1/1.03 - 1 + subsidy) therefore peaks
-- strictly inside the grid. return_per_producer_dollar does NOT: it is 1/(1-subsidy) at
-- rated experience, monotone in the subsidy, and blind to how little guarantee is left —
-- which is why guarantee_retained is stored next to it. The two columns answer different
-- questions and routinely disagree.
--
-- These numbers are FORWARD-LOOKING, off RMA's own rate draws. They cannot be backtested:
-- sobtpu reports Coverage Level as '.0000' on every plan-82 row from crop year 2008 on, so
-- realized loss ratio by deductible does not exist in any public RMA file.
CREATE TABLE IF NOT EXISTS lgm_deductible_curve (
    reinsurance_year INTEGER NOT NULL,
    commodity_code  TEXT NOT NULL,
    type_code       TEXT NOT NULL,
    state_code      TEXT NOT NULL,
    deductible      REAL NOT NULL,
    subsidy_pct     REAL,
    total_premium   REAL,              -- per unit of exposure, RMA Steps 1-5
    producer_premium REAL,
    expected_indemnity REAL,
    net_expected_gain REAL,            -- E[indemnity] - producer premium
    return_per_producer_dollar REAL,   -- loss_ratio / (1 - subsidy)
    premium_stderr  REAL,              -- Monte Carlo SE of the 500-draw mean
    expected_total_gross_margin REAL,
    gross_margin_guarantee REAL,
    guarantee_retained REAL,           -- GMG / EGM — the protection objective
    pooled          INTEGER,           -- 1 = 2+ months of target marketings (subsidy > 0)
    source          TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (reinsurance_year, commodity_code, type_code, state_code, deductible)
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added to tables that already exist in a deployed DB. CREATE TABLE IF NOT EXISTS
# is a no-op once the table is there, so a new column needs an explicit ALTER. Additive
# only (SQLite appends the column with NULLs); keep entries here forever — they are cheap
# and make an old catalog.db forward-compatible without a rebuild.
ADD_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, type) — see prf_opt_best's comment for what these hold.
    ("prf_opt_best", "best_win_rate_sum", "REAL"),
    ("prf_opt_best", "best_net_rate_sum", "REAL"),
    # The JAN-FEB percent-of-value limit the sweep applied (RMA PM-26-053: 25 from RY2027);
    # NULL = the row was swept without one, i.e. under the pre-2027 rules.
    ("prf_opt_best", "janfeb_max_pct", "INTEGER"),
    ("prf_max_pct", "janfeb_max_pct", "INTEGER"),
]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Add any ADD_COLUMNS missing from an existing DB. Returns what it added."""
    added: list[str] = []
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, col, coltype in ADD_COLUMNS:
        if table not in have:
            continue  # CREATE TABLE already carries the column
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col in cols:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        added.append(f"{table}.{col}")
    if added:
        conn.commit()
    return added


# Columns the pre-coverage-level sob_sales carried, in the order they are copied forward.
_OLD_SOB_SALES_COLS = ("year", "state", "county_fips", "crop", "commodity_code", "plan_code",
                       "plan_abbrev", "net_acres", "liability", "total_premium", "subsidy",
                       "indemnity", "policies_sold", "source", "fetched_at")


def migrate_sob_sales(conn: sqlite3.Connection) -> str | None:
    """Rebuild a pre-coverage-level sob_sales in place; return a note, or None if nothing to do.

    sob_sales gained coverage_type / coverage_level (and they joined its PRIMARY KEY) when the
    connector stopped collapsing RMA's coverage-level grain. SQLite cannot ALTER a primary key,
    and `CREATE TABLE IF NOT EXISTS` is a no-op against the old table, so an existing catalog.db
    needs this explicit rename-copy-drop. Nothing is lost: old rows carry forward with
    coverage_type 'ALL' and coverage_level 0, meaning "summed over every coverage level", and the
    next `refresh --source rma_sob --force` replaces them with the real breakout.
    """
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "sob_sales" not in have:
        return None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sob_sales)")}
    if "coverage_level" in cols:
        return None
    n = conn.execute("SELECT COUNT(*) FROM sob_sales").fetchone()[0]
    conn.execute("ALTER TABLE sob_sales RENAME TO sob_sales_pre_coverage_level")
    conn.executescript(SCHEMA)          # recreates sob_sales in the new shape
    old = ", ".join(_OLD_SOB_SALES_COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO sob_sales ({old}, coverage_type, coverage_level, "
        "producer_premium) "
        f"SELECT {old}, 'ALL', 0, COALESCE(total_premium, 0) - COALESCE(subsidy, 0) "
        "FROM sob_sales_pre_coverage_level")
    conn.execute("DROP TABLE sob_sales_pre_coverage_level")
    conn.commit()
    return f"sob_sales rebuilt with coverage_type/coverage_level ({n:,} legacy rows carried over)"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    migrate_sob_sales(conn)
    apply_migrations(conn)


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Initialize / inspect the catalog database.")
    ap.add_argument("--init", action="store_true", help="Create tables if missing.")
    ap.add_argument("--counts", action="store_true", help="Print row counts.")
    args = ap.parse_args()

    conn = connect()
    if args.init or not args.counts:
        init_db(conn)
        print(f"Schema ready at {config.DB_PATH}")
    if args.counts:
        for t in ("aips", "products", "product_crops", "product_states", "sob_sales",
                  "documents", "fetch_log"):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"{t:16} {n}")
    conn.close()


if __name__ == "__main__":
    _main()
