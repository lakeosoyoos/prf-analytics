"""PRF bulk data path: whole-country indices + rates from RMA's published FILES.

Replaces the per-grid PrfWebApi calls of src/prfdata.py for nationwide work.  The API path
needs ~6 HTTP round-trips per grid (1 index + 1 location + 5 rate calls); at 13,626 CONUS
grids that is ~82,000 requests.  RMA publishes the same numbers in bulk, so this module
gets the entire country from ONE 56 MB download plus ADM members already on disk.

TWO SOURCES
-----------
1. RAINFALL INDEX HISTORY -- one zip, refreshed by RMA each crop year:

     https://pubfs-rma.fpac.usda.gov/pub/Miscellaneous_Files/VI_RI_Data/
         Rainfall_Index_HistoricData2026CY.zip        (~56 MB -> 342 MB text)

   Pipe-delimited, one header line then 11,865,777 data rows:

     grid_id|InsurancePlanCode|Commodity0088|Commodity1191|Year|PracticeCode|ActualIndex
     10020|13|1|1|1948|625|0.755

   Commodity0088 = 1 marks the row as belonging to PRF (0088 Pasture/Rangeland/Forage);
   Commodity1191 is the Annual Forage plan that shares the same grid indices.  Despite its
   name the PracticeCode column carries the ADM A00480 INTERVAL code -- 625 = JAN-FEB
   through 635 = NOV-DEC, sequential -- which is exactly prfdata.INTERVALS.  Years run
   1948..2024 for all 14,469 grids (13,626 CONUS integer ids + 843 Hawaii 'H####' ids on a
   separate grid system, which this loader skips).  This vintage is the CANONICAL ranking
   input: every grid is scored against one internally consistent index release rather than
   whatever the API happened to serve on the day that grid was fetched.

2. PREMIUM RATES + GRID->COUNTY -- the cached ADM chain under data/cache/adm/ (no network
   when the members are already there).  Verified field-for-field against the API for grid
   27663, all 5 coverage levels x 11 intervals:

     A00810 Price rc03   PRF price rows.  Field 10 "Sub County Code" is the PRF grid id;
                         fields 8/9 give State/County Code; fields 21/22/24 the Intended
                         Use / Irrigation / Organic practice codes; field 25 the interval
                         code; field 3 the ADM Insurance Offer ID.
     A01130 AreaCoverageLevel rc05   (offer id x sub county code x coverage level) ->
                         Area Rate ID, coverage type 'A' (additional; 'C' is CAT).
     A01135 AreaRate     Area Rate ID -> Base Rate == the API's PremiumRate.

   THE OFFER IS COUNTY-GRAIN, NOT GRID-GRAIN.  One ADM Insurance Offer ID covers a whole
   county x intended use x interval; A00810 repeats it once per grid in that county, and
   A01130 breaks it back out per grid in its Sub County Code column.  So the offer supplies
   the (use, interval) of a rate and A01130's SUB COUNTY CODE supplies the GRID -- reading
   the grid off the A00810 row instead would smear one grid's rate across every grid in the
   county.  A grid that straddles a county line is reachable through either county's offer;
   the rate is a property of the grid, so those collapse onto one prf_grid_rate row and a
   running sample check MEASURES any county-to-county disagreement instead of assuming it away.

CHANGE DETECTION
----------------
window_hash() fingerprints a grid's 2006-2024 index window; changed_grids() diffs those
fingerprints against prf_index_hash.  RMA revises the index history as gauge data is
finalized, but only a handful of grids move in a given month -- so the monthly job can
re-score the movers instead of all 13,626 grids (see scripts/monthly_update.sh).

POLITENESS: exactly one network call (the zip), skipped entirely once cached.  The ADM
members are read from disk and never re-fetched here.

CLI:
    .venv/bin/python -m src.prfbulk --indices --rates [--force] [--min-year 1948]
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, prfdata

# --- source 1: the rainfall-index bulk zip ---------------------------------
#
# THE PROVIDER CHANGED FOR 2027 AND THE HISTORY WAS RE-BASED WITH IT.
# Through the 2026 crop year RMA built the Rainfall Index on NOAA CPC gridded precipitation.
# For 2027 (PRF contract change date 2026-08-31) it moved to NOAA NCEI nClimGrid, rescaled
# to the same 0.25-degree lattice, and republished the ENTIRE history on the new basis with
# the normal period starting 1951 instead of 1948.
#
# This is not a refresh, it is a different series, and the difference is not small. Measured
# here, the 2027CY file against our stored 2026CY vintage over 2,951,080 overlapping
# grid x year x interval cells for 2006-2025:
#
#     identical             0.46%
#     median |difference|   0.099
#     49.7% of cells move by more than 0.10 of normal
#
# A 0.10 move in the index is a 0.11 move in the indemnity per $1 of protection at 90%
# coverage, so interval rankings genuinely reorder. Scoring a 2027 policy on CPC numbers
# would be advice about a series RMA no longer pays on. Hence CROP_YEAR below and the
# provider tag baked into index_source(): the vintage travels with every row, and
# index_vintages() lets any consumer refuse a table holding more than one.
#
# The lattice itself is unchanged (0.25 degrees, stride 300, origin -130/20), but the grid
# ROSTER is not: 13,626 numeric grids in 2026CY, 13,533 in 2027CY.
#
# Ref: RMA "Rainfall Index Data - 2027 and Beyond Processing Instructions" (2026-06); RMA
# news release 2026-04-30 announcing the NCEI move.
CROP_YEAR = 2027
PROVIDER_BY_CROP_YEAR = {2026: "cpc", 2027: "ncei"}
# RMA's normal period for 2027 starts 1951; the 2027CY file has no 1948-1950 rows at all.
FIRST_YEAR_BY_CROP_YEAR = {2026: 1948, 2027: 1951}

BULK_DIR = config.CACHE_DIR / "prf" / "bulk"
_BULK_BASE = ("https://pubfs-rma.fpac.usda.gov/pub/Miscellaneous_Files/VI_RI_Data/"
              "Rainfall_Index_HistoricData{cy}CY.zip")


def provider(crop_year: int = CROP_YEAR) -> str:
    """'cpc' or 'ncei' — which precipitation product built this crop year's index."""
    return PROVIDER_BY_CROP_YEAR.get(crop_year, "ncei" if crop_year >= 2027 else "cpc")


def bulk_url(crop_year: int = CROP_YEAR) -> str:
    return _BULK_BASE.format(cy=crop_year)


def bulk_zip(crop_year: int = CROP_YEAR) -> Path:
    return BULK_DIR / f"Rainfall_Index_HistoricData{crop_year}CY.zip"


def index_source(crop_year: int = CROP_YEAR) -> str:
    """The `source` stamped on every prf_grid_index row: crop year AND provider.

    Both halves matter. The crop year says which RMA release a row came from; the provider
    says which precipitation product is underneath, which is the thing that makes two
    releases incomparable.
    """
    return f"vi_ri_{crop_year}cy_{provider(crop_year)}"


def index_vintages(conn) -> list[str]:
    """Distinct `source` values in prf_grid_index."""
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM prf_grid_index WHERE source IS NOT NULL "
            "ORDER BY source")]
    except Exception:
        return []


def provider_of_source(source: str) -> str:
    """Which precipitation product a stored `source` string came from.

    The test is the PROVIDER, not the string. Several source strings can name the same
    series — the 2026CY bulk file and the PrfWebApi top-up that added 2025 to it are both
    NOAA CPC, retrieved two ways, and mixing them is fine. Mixing CPC with NCEI is not.
    Only the 2027CY release and later are NCEI, and only those carry the tag.
    """
    return "ncei" if "ncei" in (source or "").lower() else "cpc"


def index_providers(conn) -> set[str]:
    """The distinct precipitation products present in prf_grid_index.

    More than one means the table spans RMA's 2027 source change, and any history read out
    of it crosses a seam where about half the cells move by more than 0.10 of normal.
    """
    return {provider_of_source(s) for s in index_vintages(conn)}


# Back-compat for callers written before the crop year became a parameter.
BULK_URL = bulk_url()
BULK_ZIP = bulk_zip()
INDEX_SOURCE = index_source()

# --- source 2: the ADM chain ----------------------------------------------
ADM_DIR = config.CACHE_DIR / "adm"

# A00810 Price, 0-based field offsets (header in the file itself).
P_REC, P_CAT, P_OFFER, P_RY = 0, 1, 2, 3
P_COMMODITY, P_PLAN, P_STATE, P_COUNTY, P_GRID = 5, 6, 7, 8, 9
P_USE, P_IRR, P_ORG, P_INTERVAL = 20, 21, 23, 24

# A01130 AreaCoverageLevel, 0-based.
C_REC, C_CAT, C_RY, C_OFFER, C_SUBCOUNTY = 0, 1, 2, 3, 4
C_COVERAGE, C_COVTYPE, C_RATEID = 6, 7, 12

# A01135 AreaRate, 0-based.
R_REC, R_RY, R_RATEID, R_BASERATE = 0, 2, 3, 5

PRF_COMMODITY = "0088"
PRF_PLAN = "13"
ADDITIONAL_COVERAGE = "A"       # 'C' = CAT, which the optimizer does not score

# Scoring window shared with prfsweep (kept literal here so prfbulk has no import cycle).
YEARS = tuple(range(2006, 2025))

DEFAULT_USES = ("Grazing", "Haying", "Haying-Irrigated")

# PracticeCode in the bulk file == ADM interval code.
INTERVAL_BY_CODE = dict(prfdata.INTERVALS)

BATCH = 100_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    """Date-grain stamp for bulk rows.

    prf_grid_index gets ~11.5M rows from one file read; a full ISO timestamp on each
    would add ~200 MB of identical text to the database for no provenance value that the
    date and the `source` column do not already carry.
    """
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Download (the module's only network call)
# ---------------------------------------------------------------------------

def ensure_bulk_zip(force: bool = False, cfg: config.Config | None = None,
                    log=print) -> Path:
    """Fetch the VI/RI history zip into data/cache/prf/bulk/ once. Returns its path."""
    if BULK_ZIP.exists() and not force:
        return BULK_ZIP
    import requests  # local import: the parse path must work without network deps

    cfg = cfg or config.load()
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    log(f"downloading {BULK_URL}")
    tmp = BULK_ZIP.with_suffix(".part")
    with requests.get(BULK_URL, headers={"User-Agent": cfg.user_agent},
                      stream=True, timeout=cfg.timeout_seconds) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
    tmp.replace(BULK_ZIP)
    log(f"  saved {BULK_ZIP} ({BULK_ZIP.stat().st_size / 1e6:.1f} MB)")
    return BULK_ZIP


# ---------------------------------------------------------------------------
# Pure parsing (unit-tested, no network, no DB)
# ---------------------------------------------------------------------------

def parse_index_line(line: str, min_year: int | None = None,
                     max_year: int | None = None):
    """One bulk data line -> (grid_id, year, 'JAN-FEB', 0.755), or None.

    Returns None (rather than raising) for every legitimate reason to drop a row:
    the header, blank lines, non-PRF rows (Commodity0088 != 1 or plan != 13), Hawaii
    'H####' grid ids, unknown interval codes, blank index values, out-of-window years.
    A malformed line -- right shape, unparseable numbers -- also yields None; the
    caller counts those separately so a format change surfaces as a row-count drop
    rather than a silent halt.
    """
    f = line.rstrip("\r\n").split("|")
    if len(f) < 7:
        return None
    grid, plan, c0088, _c1191, year, practice, value = f[:7]
    if plan != PRF_PLAN or c0088 != "1":
        return None
    if not grid.isdigit():            # 'H####' Hawaii grid system, or the header row
        return None
    name = INTERVAL_BY_CODE.get(practice)
    if name is None or not value:
        return None
    try:
        y = int(year)
        v = float(value)
    except ValueError:
        return None
    if (min_year is not None and y < min_year) or (max_year is not None and y > max_year):
        return None
    return int(grid), y, name, v


def iter_bulk_indices(zip_path: Path | str | None = None, min_year: int | None = None,
                      max_year: int | None = None, stats: dict | None = None):
    """Stream the zip and yield parsed index rows.

    The 342 MB member is read through zipfile's decompressing file object a line at a
    time -- it is never materialized in memory.
    """
    zp = Path(zip_path or BULK_ZIP)
    with zipfile.ZipFile(zp) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        with zf.open(member) as raw:
            for i, bline in enumerate(raw):
                if i == 0:
                    continue                      # header
                row = parse_index_line(bline.decode("utf-8", "replace"),
                                       min_year, max_year)
                if row is None:
                    if stats is not None:
                        stats["skipped"] = stats.get("skipped", 0) + 1
                    continue
                if stats is not None:
                    stats["kept"] = stats.get("kept", 0) + 1
                yield row


def _adm_files(stem: str, adm_dir: Path | None = None) -> list[Path]:
    """Every cached member for an ADM record type, oldest release first.

    Sorted so that a later daily release overwrites an earlier one on the same key --
    RMA ships A01130/A01135 as dated daily extracts, and the newest one is authoritative.
    """
    adm_dir = Path(adm_dir or ADM_DIR)
    hits = sorted(p for p in adm_dir.glob(f"*{stem}*.txt"))
    if not hits:
        # This module READS the ADM cache and never populates it, so a bare "no member"
        # traceback is a dead end. It was hit for real when A00810_Price (1.1 GB) went
        # missing from the cache: the rates pass died halfway with no indication that the
        # fix is a different command entirely. data/cache/ is gitignored, so a fresh clone
        # starts in exactly this state. Name the remedy rather than just the symptom.
        raise FileNotFoundError(
            f"no {stem} member under {adm_dir}.\n"
            f"src.prfbulk reads the ADM cache but does not download it. Populate it with:\n"
            f"    .venv/bin/python -m src.refresh "
            f"--source rma_adm,prf_adm --force --no-enrich\n"
            f"then re-run this command.")
    return hits


def _use_lookup(uses=DEFAULT_USES) -> dict[tuple[str, str, str], str]:
    """{('007','997','997'): 'Grazing', ...} from prfdata.USE_PARAMS."""
    return {prfdata.USE_PARAMS[u]: u for u in uses}


def iter_price_offers(path: Path, year: int, uses=DEFAULT_USES):
    """A00810 rc03 -> (offer_id:int, grid:int, state_code, county_code, use, interval)."""
    by_codes = _use_lookup(uses)
    with open(path) as fh:
        next(fh)
        for line in fh:
            f = line.split("|")
            if len(f) <= P_INTERVAL:
                continue
            if (f[P_REC] != "A00810" or f[P_CAT] != "03"
                    or f[P_COMMODITY] != PRF_COMMODITY or f[P_PLAN] != PRF_PLAN
                    or f[P_RY] != str(year)):
                continue
            grid = f[P_GRID].strip()
            if not grid.isdigit():
                continue
            use = by_codes.get((f[P_USE], f[P_IRR], f[P_ORG]))
            if use is None:
                continue
            name = INTERVAL_BY_CODE.get(f[P_INTERVAL])
            if name is None:
                continue
            offer = f[P_OFFER].strip()
            if not offer.isdigit():
                continue
            yield int(offer), int(grid), f[P_STATE].strip(), f[P_COUNTY].strip(), use, name


def load_area_rates(year: int, adm_dir: Path | None = None) -> dict[int, float]:
    """A01135 Area Rate ID -> Base Rate, merged across cached daily releases."""
    out: dict[int, float] = {}
    for path in _adm_files("A01135_AreaRate", adm_dir):
        with open(path) as fh:
            next(fh)
            for line in fh:
                f = line.split("|")
                if len(f) <= R_BASERATE or f[R_REC] != "A01135" or f[R_RY] != str(year):
                    continue
                rid, rate = f[R_RATEID].strip(), f[R_BASERATE].strip()
                if not rid.isdigit() or not rate:
                    continue
                try:
                    out[int(rid)] = float(rate)
                except ValueError:
                    continue
    return out


def iter_area_coverage(path: Path, year: int):
    """A01130 rc05 additional-coverage rows -> (offer_id, grid_id, coverage, area_rate_id).

    grid_id is the row's Sub County Code -- the field that turns a county-grain offer back
    into per-grid rates. Non-numeric (Hawaii-system) sub county codes are skipped.
    """
    with open(path) as fh:
        next(fh)
        for line in fh:
            f = line.split("|")
            if len(f) <= C_RATEID:
                continue
            if (f[C_REC] != "A01130" or f[C_CAT] != "05" or f[C_RY] != str(year)
                    or f[C_COVTYPE] != ADDITIONAL_COVERAGE):
                continue
            offer, rid, cov = f[C_OFFER].strip(), f[C_RATEID].strip(), f[C_COVERAGE].strip()
            grid = f[C_SUBCOUNTY].strip()
            if not offer.isdigit() or not rid.isdigit() or not cov or not grid.isdigit():
                continue
            try:
                c = float(cov)
            except ValueError:
                continue
            yield int(offer), int(grid), c, int(rid)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_IDX_SQL = """INSERT INTO prf_grid_index
                (grid_id, year, interval_code, index_value, source, fetched_at)
              VALUES (?,?,?,?,?,?)
              ON CONFLICT(grid_id, year, interval_code) DO UPDATE SET
                index_value=excluded.index_value, source=excluded.source,
                fetched_at=excluded.fetched_at"""


class _BulkPragmas:
    """Speed pragmas for a multi-million-row load, restored on exit.

    journal_mode=OFF / synchronous=OFF trade crash-durability for throughput. That is the
    right trade here and only here: prf_grid_index is a pure derivative of a file on disk,
    so a crash mid-load costs a re-run, not data.
    """

    def __init__(self, conn):
        self.conn = conn
        self.journal = None

    def __enter__(self):
        self.journal = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.execute("PRAGMA cache_size = -400000")   # ~400 MB page cache
        return self

    def __exit__(self, *exc):
        self.conn.commit()
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.execute(f"PRAGMA journal_mode = {self.journal or 'delete'}")
        self.conn.execute("PRAGMA cache_size = -2000")
        return False


def load_indices(conn: sqlite3.Connection, force: bool = False,
                 zip_path: Path | str | None = None, min_year: int | None = None,
                 max_year: int | None = None, cfg: config.Config | None = None,
                 log=print) -> dict:
    """Populate prf_grid_index for every CONUS grid from the bulk zip.

    Idempotent: re-running upserts the same values. Skips the whole job when the table
    already carries the bulk vintage, unless force. Returns a stats dict.
    """
    cfg = cfg or config.load()
    have = conn.execute("SELECT COUNT(*) FROM prf_grid_index WHERE source = ?",
                        (INDEX_SOURCE,)).fetchone()[0]
    if have and not force:
        log(f"prf_grid_index already holds {have:,} {INDEX_SOURCE} rows; use --force to reload")
        return {"rows": 0, "already": have, "skipped_job": True}

    zp = Path(zip_path) if zip_path else ensure_bulk_zip(cfg=cfg, log=log)
    stamp = _today()
    stats: dict = {"kept": 0, "skipped": 0}
    t0 = time.monotonic()
    written = 0
    batch: list[tuple] = []
    grids: set[int] = set()

    with _BulkPragmas(conn):
        for grid, year, iv, val in iter_bulk_indices(zp, min_year, max_year, stats):
            grids.add(grid)
            batch.append((grid, year, iv, val, INDEX_SOURCE, stamp))
            if len(batch) >= BATCH:
                conn.executemany(_IDX_SQL, batch)
                written += len(batch)
                batch.clear()
                if written % (BATCH * 10) == 0:
                    log(f"  {written:,} index rows ({len(grids):,} grids, "
                        f"{time.monotonic() - t0:.0f}s)")
        if batch:
            conn.executemany(_IDX_SQL, batch)
            written += len(batch)

    lo, hi = conn.execute("SELECT MIN(year), MAX(year) FROM prf_grid_index "
                          "WHERE source = ?", (INDEX_SOURCE,)).fetchone()
    out = {"rows": written, "grids": len(grids), "years": (lo, hi),
           "non_prf_or_hawaii_lines": stats["skipped"],
           "elapsed_s": time.monotonic() - t0}
    log(f"prf_grid_index: {written:,} rows / {len(grids):,} CONUS grids, years {lo}-{hi}, "
        f"{out['elapsed_s'] / 60:.1f} min")
    return out


_RATE_SQL = """INSERT INTO prf_grid_rate
                 (grid_id, year, intended_use, interval_code, coverage_level,
                  premium_rate, source, fetched_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(grid_id, year, intended_use, interval_code, coverage_level)
               DO UPDATE SET premium_rate=excluded.premium_rate,
                 source=excluded.source, fetched_at=excluded.fetched_at"""

_COUNTY_SQL = """INSERT INTO prf_grid_county (grid_id, state, county_fips, county_name, source)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(grid_id, county_fips) DO UPDATE SET
                   state=excluded.state, county_name=excluded.county_name,
                   source=excluded.source"""


def load_rates_counties(conn: sqlite3.Connection, force: bool = False,
                        uses=DEFAULT_USES, adm_dir: Path | None = None,
                        cfg: config.Config | None = None,
                        coverage_levels=prfdata.COVERAGE_LEVELS,
                        sample_mod: int = 101, log=print) -> dict:
    """Populate prf_grid_rate + prf_grid_county for every CONUS grid from the ADM chain.

    Three streamed passes, no network: A01135 into a rate-id -> base-rate map, A00810
    rc03 into an offer-id -> (use, interval) map plus the grid -> county rows, then
    A01130 rc05 -- whose Sub County Code carries the GRID -- joining the two straight
    into batched upserts.

    A grid on a county line is offered through both counties, so two offers can write the
    same prf_grid_rate row. Every grid whose id is a multiple of `sample_mod` is kept in
    an exact-value map so any such disagreement is MEASURED (reported as
    `county_rate_conflicts`) rather than assumed away.
    """
    cfg = cfg or config.load()
    year = cfg.reinsurance_year
    src = f"adm_{year}"
    have = conn.execute("SELECT COUNT(*) FROM prf_grid_rate WHERE source = ?",
                        (src,)).fetchone()[0]
    if have and not force:
        log(f"prf_grid_rate already holds {have:,} {src} rows; use --force to reload")
        return {"rate_rows": 0, "already": have, "skipped_job": True}

    t0 = time.monotonic()
    adm_dir = Path(adm_dir or ADM_DIR)
    stamp = _now_iso()
    covs = {round(c, 4) for c in coverage_levels}

    log("pass 1/3: A01135 AreaRate")
    base_rate = load_area_rates(year, adm_dir)
    log(f"  {len(base_rate):,} area rate ids")

    log("pass 2/3: A00810 Price rc03 (offers + grid->county)")
    # Deferred import: prfsweep imports prfbulk at module scope for changed_grids(), so
    # this direction has to stay lazy. Both are pure ADM code-table readers, no cycle at
    # call time.
    from .prfsweep import state_codes, county_names
    abbrev = {v: k for k, v in state_codes(adm_dir).items()}
    names = county_names(adm_dir)
    use_idx = {u: i for i, u in enumerate(uses)}
    iv_idx = {iv: i for i, iv in enumerate(prfdata.INTERVAL_ORDER)}
    unpack_use = {i: u for u, i in use_idx.items()}
    unpack_iv = {i: iv for iv, i in iv_idx.items()}

    offers: dict[int, int] = {}
    counties: dict[tuple[int, str], tuple] = {}
    for path in _adm_files("A00810_Price", adm_dir):
        for offer, grid, sc, cc, use, iv in iter_price_offers(path, year, uses):
            offers[offer] = use_idx[use] * 16 + iv_idx[iv]
            fips = f"{sc}{cc}"
            key = (grid, fips)
            if key not in counties:
                counties[key] = (grid, abbrev.get(sc, sc), fips,
                                 names.get((sc, cc), ""), src)
    log(f"  {len(offers):,} PRF offers (county x use x interval), "
        f"{len(counties):,} grid-county pairs "
        f"({len({g for g, _ in counties}):,} grids)")

    conn.executemany(_COUNTY_SQL, list(counties.values()))
    conn.commit()

    log("pass 3/3: A01130 AreaCoverageLevel rc05 -> prf_grid_rate")
    sample: dict[tuple, float] = {}
    conflicts = 0
    missing_rate_id = 0
    written = 0
    batch: list[tuple] = []
    with _BulkPragmas(conn):
        for path in _adm_files("A01130_AreaCoverageLevel", adm_dir):
            for offer, grid, cov, rate_id in iter_area_coverage(path, year):
                packed = offers.get(offer)
                if packed is None or round(cov, 4) not in covs:
                    continue
                rate = base_rate.get(rate_id)
                if rate is None:
                    missing_rate_id += 1
                    continue
                iv = unpack_iv[packed % 16]
                use = unpack_use[packed // 16]
                if sample_mod and grid % sample_mod == 0:
                    k = (grid, use, iv, round(cov, 4))
                    prev = sample.get(k)
                    if prev is None:
                        sample[k] = rate
                    elif abs(prev - rate) > 1e-12:
                        conflicts += 1
                batch.append((grid, year, use, iv, cov, rate, src, stamp))
                if len(batch) >= BATCH:
                    conn.executemany(_RATE_SQL, batch)
                    written += len(batch)
                    batch.clear()
                    log(f"  {written:,} rate upserts ({time.monotonic() - t0:.0f}s)")
            if batch:
                conn.executemany(_RATE_SQL, batch)
                written += len(batch)
                batch.clear()

    prfdata.ensure_subsidy(conn)
    out = {
        "rate_upserts": written,
        "rate_rows": conn.execute("SELECT COUNT(*) FROM prf_grid_rate WHERE source = ?",
                                  (src,)).fetchone()[0],
        "rate_grids": conn.execute("SELECT COUNT(DISTINCT grid_id) FROM prf_grid_rate "
                                   "WHERE source = ?", (src,)).fetchone()[0],
        "county_rows": len(counties),
        "county_grids": len({g for g, _ in counties}),
        "county_rate_conflicts": conflicts,
        "county_rate_sampled": len(sample),
        "missing_area_rate_id": missing_rate_id,
        "uses": list(uses),
        "elapsed_s": time.monotonic() - t0,
    }
    log(f"prf_grid_rate: {out['rate_rows']:,} rows / {out['rate_grids']:,} grids; "
        f"prf_grid_county: {out['county_rows']:,} rows / {out['county_grids']:,} grids; "
        f"{out['elapsed_s'] / 60:.1f} min")
    if conflicts:
        log(f"  WARNING: {conflicts} cross-county rate disagreements in the "
            f"{out['county_rate_sampled']} sampled grid/use/interval/coverage cells")
    return out


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _hash_rows(rows) -> str:
    h = hashlib.sha256()
    for year, iv, val in rows:
        h.update(f"{year}|{iv}|{val:.6f}\n".encode())
    return h.hexdigest()


def window_hash(conn: sqlite3.Connection, grid_id: int, years=YEARS) -> str | None:
    """SHA-256 of one grid's ordered 2006-2024 index window, or None when it has none."""
    rows = conn.execute(
        """SELECT year, interval_code, index_value FROM prf_grid_index
           WHERE grid_id = ? AND year BETWEEN ? AND ?
           ORDER BY year, interval_code""",
        (grid_id, min(years), max(years))).fetchall()
    if not rows:
        return None
    return _hash_rows((r[0], r[1], r[2]) for r in rows)


def current_hashes(conn: sqlite3.Connection, years=YEARS) -> dict[int, str]:
    """{grid_id: window_hash} for every grid in prf_grid_index, in one ordered scan."""
    out: dict[int, str] = {}
    cur_grid = None
    rows: list[tuple] = []
    for g, y, iv, v in conn.execute(
            """SELECT grid_id, year, interval_code, index_value FROM prf_grid_index
               WHERE year BETWEEN ? AND ?
               ORDER BY grid_id, year, interval_code""",
            (min(years), max(years))):
        if g != cur_grid:
            if cur_grid is not None:
                out[cur_grid] = _hash_rows(rows)
            cur_grid, rows = g, []
        rows.append((y, iv, v))
    if cur_grid is not None:
        out[cur_grid] = _hash_rows(rows)
    return out


def stored_hashes(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT grid_id, window_hash FROM prf_index_hash")}


def changed_grids(conn: sqlite3.Connection, grid_ids=None, years=YEARS) -> list[int]:
    """Grids whose 2006-2024 window differs from (or is missing in) prf_index_hash.

    A grid never hashed before counts as changed -- that is what makes the first
    --changed-only run sweep everything and later ones sweep only the movers.
    """
    cur = current_hashes(conn, years)
    old = stored_hashes(conn)
    want = None if grid_ids is None else {int(g) for g in grid_ids}
    return sorted(g for g, h in cur.items()
                  if (want is None or g in want) and old.get(g) != h)


def update_hashes(conn: sqlite3.Connection, grid_ids=None, years=YEARS) -> int:
    """Record the current window hash for the given grids (all grids when None)."""
    cur = current_hashes(conn, years)
    if grid_ids is not None:
        want = {int(g) for g in grid_ids}
        cur = {g: h for g, h in cur.items() if g in want}
    now = _now_iso()
    conn.executemany(
        """INSERT INTO prf_index_hash (grid_id, window_hash, updated_at) VALUES (?,?,?)
           ON CONFLICT(grid_id) DO UPDATE SET
             window_hash=excluded.window_hash, updated_at=excluded.updated_at""",
        [(g, h, now) for g, h in cur.items()])
    conn.commit()
    return len(cur)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Load PRF indices + rates nationwide from RMA bulk files.")
    ap.add_argument("--indices", action="store_true",
                    help="load prf_grid_index from the VI/RI history zip")
    ap.add_argument("--rates", action="store_true",
                    help="load prf_grid_rate + prf_grid_county from the cached ADM chain")
    ap.add_argument("--hashes", action="store_true",
                    help="(re)write prf_index_hash for every grid")
    ap.add_argument("--force", action="store_true",
                    help="reload even when the bulk vintage is already present")
    ap.add_argument("--min-year", type=int, default=None,
                    help="drop index rows before this year (smaller DB; the optimizer "
                         "only reads 2006-2024)")
    ap.add_argument("--max-year", type=int, default=None)
    ap.add_argument("--use", action="append", default=None,
                    choices=sorted(prfdata.USE_PARAMS),
                    help="intended use(s) for --rates (repeatable; default all three)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    if not (args.indices or args.rates or args.hashes):
        ap.error("nothing to do: pass --indices and/or --rates (and/or --hashes)")

    conn = db.connect(args.db)
    db.init_db(conn)
    try:
        if args.indices:
            load_indices(conn, force=args.force, min_year=args.min_year,
                         max_year=args.max_year)
        if args.rates:
            load_rates_counties(conn, force=args.force,
                                uses=tuple(args.use) if args.use else DEFAULT_USES)
        if args.hashes or args.indices:
            n = update_hashes(conn)
            print(f"prf_index_hash: {n:,} grid fingerprints")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
