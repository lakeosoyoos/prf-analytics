"""Compact per-grid PRF interval history — the data behind the interval analyzer.

WHY THIS TABLE EXISTS
---------------------
The interval analyzer (src/prfintervalpage.py) needs, for ONE grid at a time, the rainfall
index for every interval x year plus that grid's premium rates, so it can recompute win
rate / return per $1 / return per acre over any lookback window the user drags to, and
re-run the allocation optimizer against the result. Those inputs live in prf_grid_index
(11.7M rows) and prf_grid_rate (2.1M rows), and scripts/build_app_db.py drops both from the
shipped database — together they are ~1 GB against a 95 MB ship budget.

So this module reduces them to one row per grid of packed bytes: 13,462 rows, ~8.4 MB,
which does ship. Nothing is aggregated away — every interval x year cell survives at RMA's own precision — only
the *year span* is bounded, deliberately:

YEAR SPAN is 2000-2025 (26 years), deeper than src/prfsweep.YEARS (2006-2025) on purpose. The
analyzer lets the reader pick where the lookback ENDS as well as how long it is (5/10/15/20
years), so a 20-year window ending in 2019 needs index years back to 2000. The stored sweep
(prf_opt_best) still covers 2006-2025 only; the page compares against it only when the chosen
window is exactly that one, and says so otherwise. The span is a build-time constant: the
working catalog holds the NCEI history back to 1951, but each added year costs 22 bytes a grid
(~0.3 MB across 13,414 grids) against a 95 MB ship budget. 1986 was tried first (+3.7 MB, 92.4 MB
shipped); 2000 was chosen to fit the budget (94.6 MB shipped once the index went exact), at the price that a 20-year lookback can
then end only in 2019-2025. Widening it is one constant here plus a rebuild of the table.

INDEX PRECISION is EXACT: two bytes per cell, the index in whole thousandths of normal, which is
the precision RMA publishes it at (0.345, 1.526). So every payout, average, win rate and rank the
analyzer computes is the one RMA's own numbers give -- there is no rounding to reason about.
Index format 2 packed one byte per cell (0.005 steps below 0.90, coarser above) to save ~3.9 MB;
that rounding could swap two nearly tied allocations, so it was retired for the exact code:

    uint16 little-endian, value = round(index * 1000)   (0.000 .. 65.534)
    65535                                                missing

Rates are stored the same way, as exact 1/10,000ths.

RATES ARE USE-INDEPENDENT, measured not assumed: across all 725,020 overlapping
(grid, interval, coverage) rows, Grazing, Haying and Haying-Irrigated carry the IDENTICAL
premium rate -- zero mismatches. That is the same fact src/prfsweep and build_app_db's
dedupe_prf_opt_best already rest on (a PRF rate is a function of the grid's rainfall
distribution, the interval and the coverage level, not of what the forage is used for), so
one rate table serves all three uses. What DOES differ is availability: Haying is simply not
offered on some grid/interval pairs, and absence there means "not sold", never "same as
Grazing" -- so a per-use availability mask is stored alongside, and the analyzer refuses to
recommend an interval the selected use cannot buy.
"""
from __future__ import annotations

import sqlite3
import struct
from typing import Iterator

from .prfopt import INTERVALS

# The packed span. Deeper than the sweep's own window (2006-2025) so the analyzer can end a
# lookback earlier than the latest year. See the module docstring.
YEAR_MIN = 2000
YEAR_MAX = 2025
YEARS = tuple(range(YEAR_MIN, YEAR_MAX + 1))

COVERAGES = (0.70, 0.75, 0.80, 0.85, 0.90)
USES = ("Grazing", "Haying", "Haying-Irrigated")

# Index storage: exact, uint16 thousandths of normal (RMA publishes 3 decimals).
INDEX_SCALE = 1000
INDEX_STEP = 1 / INDEX_SCALE   # the published precision, so storage loses nothing
INDEX_MISSING = 65535
INDEX_CAP = (INDEX_MISSING - 1) / INDEX_SCALE   # 65.534; never reached by a real index
INDEX_FORMAT = 3               # first byte of every packed index blob; 2 was the 1-byte code

RATE_SCALE = 10000             # rates are published to 4 decimals; this is exact
RATE_MISSING = 65535
MEAN_SCALE = 1000
MEAN_MISSING = 65535


def encode_index(value: float | None) -> int:
    """Index (percent of normal, 1.0 = normal) -> uint16 thousandths. See module docstring."""
    if value is None:
        return INDEX_MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return INDEX_MISSING
    if v < 0 or v != v:
        return INDEX_MISSING
    return min(INDEX_MISSING - 1, int(round(v * INDEX_SCALE)))


def decode_index(code: int) -> float | None:
    """uint16 -> index, or None when missing."""
    if code == INDEX_MISSING:
        return None
    return code / INDEX_SCALE


def pack_index(matrix: dict[int, dict[str, float]]) -> bytes:
    """{year: {interval: index}} -> INDEX_FORMAT byte + interval-major uint16s over YEARS."""
    out = bytearray([INDEX_FORMAT])
    for iv in INTERVALS:
        for year in YEARS:
            out += struct.pack("<H", encode_index(matrix.get(year, {}).get(iv)))
    return bytes(out)


def unpack_index(blob: bytes) -> dict[str, list[float | None]]:
    """Inverse of pack_index: {interval: [index per year, oldest first]}.

    The year count comes from the blob itself, so a table packed over a different span still
    reads correctly. A blob in an older format is REFUSED rather than decoded: format 2 stored
    one byte per cell on a different scale, and a wrong index that looks plausible is worse
    than an error.
    """
    n_iv = len(INTERVALS)
    if not blob or blob[0] != INDEX_FORMAT or (len(blob) - 1) % (2 * n_iv):
        raise ValueError(
            "prf_interval_history.index_blob is not in index format "
            f"{INDEX_FORMAT} -- rebuild it with scripts/build_prf_interval_history.py")
    n = (len(blob) - 1) // (2 * n_iv)
    codes = struct.unpack(f"<{n * n_iv}H", blob[1:])
    return {iv: [decode_index(c) for c in codes[i * n:(i + 1) * n]]
            for i, iv in enumerate(INTERVALS)}


def pack_means(matrix: dict[int, dict[str, float]]) -> bytes:
    """Per-interval TRUE mean index over ALL of YEARS, uint16 at 1/1000.

    The analyzer no longer reads this: its "Avg index" row follows the window the reader picks,
    so it averages the decoded cells instead (the tail of the index code is what makes that
    accurate -- see the module docstring). The column stays so the schema, and databases already
    shipped, remain interchangeable.
    """
    out = bytearray()
    for iv in INTERVALS:
        vals = [matrix[y][iv] for y in YEARS
                if y in matrix and matrix[y].get(iv) is not None]
        if not vals:
            out += struct.pack("<H", MEAN_MISSING)
            continue
        mean = sum(vals) / len(vals)
        code = min(MEAN_MISSING - 1, int(round(mean * MEAN_SCALE)))
        out += struct.pack("<H", code)
    return bytes(out)


def unpack_means(blob: bytes) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for i, iv in enumerate(INTERVALS):
        (code,) = struct.unpack_from("<H", blob, i * 2)
        out[iv] = None if code == MEAN_MISSING else code / MEAN_SCALE
    return out


def pack_rates(rates: dict[float, dict[str, float]]) -> bytes:
    """{coverage: {interval: rate}} -> interval-major uint16 at 1/10,000 (exact)."""
    out = bytearray()
    for iv in INTERVALS:
        for cov in COVERAGES:
            rate = rates.get(cov, {}).get(iv)
            if rate is None:
                out += struct.pack("<H", RATE_MISSING)
            else:
                out += struct.pack("<H", min(RATE_MISSING - 1,
                                             int(round(float(rate) * RATE_SCALE))))
    return bytes(out)


def unpack_rates(blob: bytes) -> dict[str, dict[str, float | None]]:
    """Inverse of pack_rates: {interval: {'0.9': rate, ...}} — string coverage keys for JSON."""
    out: dict[str, dict[str, float | None]] = {}
    for i, iv in enumerate(INTERVALS):
        row: dict[str, float | None] = {}
        for j, cov in enumerate(COVERAGES):
            (code,) = struct.unpack_from("<H", blob, (i * len(COVERAGES) + j) * 2)
            row[f"{cov:g}"] = None if code == RATE_MISSING else code / RATE_SCALE
        out[iv] = row
    return out


def pack_use_mask(offered: dict[str, set[str]]) -> int:
    """{use: {intervals offered}} -> bitmask, bit (use_index * 11 + interval_index)."""
    mask = 0
    for u, use in enumerate(USES):
        for i, iv in enumerate(INTERVALS):
            if iv in offered.get(use, ()):  # absence means NOT SOLD, never "same as Grazing"
                mask |= 1 << (u * len(INTERVALS) + i)
    return mask


def unpack_use_mask(mask: int) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for u, use in enumerate(USES):
        out[use] = [iv for i, iv in enumerate(INTERVALS)
                    if mask >> (u * len(INTERVALS) + i) & 1]
    return out


# ---------------------------------------------------------------------------
# Build (reads the heavy local-only tables; writes the shippable one)
# ---------------------------------------------------------------------------

def build_rows(conn: sqlite3.Connection, index_source: str | None = None) -> Iterator[tuple]:
    """Yield prf_interval_history tuples, one per grid that has index AND rate data.

    `index_source` is stamped on every row so the shipped data carries its own vintage (see
    src/db.py). Defaults to whatever prf_grid_index actually holds.

    Two ordered streaming scans merged by grid_id, NOT a query per grid: the per-grid form
    took ~9 minutes on 13,462 grids because each of the 27k lookups re-descended the index
    b-tree. Nothing is held in memory beyond the grid being packed.
    """
    if index_source is None:
        srcs = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM prf_grid_index WHERE source IS NOT NULL")]
        index_source = "+".join(sorted(srcs)) if srcs else None

    def grouped(sql: str, params: tuple = ()) -> Iterator[tuple[int, list]]:
        """Stream an ORDER BY grid_id query as (grid_id, rows-for-that-grid)."""
        cur = conn.execute(sql, params)
        current: int | None = None
        batch: list = []
        for row in cur:
            gid = row[0]
            if gid != current:
                if current is not None:
                    yield current, batch
                current, batch = gid, []
            batch.append(row[1:])
        if current is not None:
            yield current, batch

    idx_stream = grouped(
        "SELECT grid_id, year, interval_code, index_value FROM prf_grid_index "
        "WHERE year BETWEEN ? AND ? ORDER BY grid_id", (YEAR_MIN, YEAR_MAX))
    rate_stream = grouped(
        "SELECT grid_id, year, intended_use, interval_code, coverage_level, premium_rate "
        "FROM prf_grid_rate ORDER BY grid_id")

    rates_next = next(rate_stream, None)
    for grid_id, idx_rows in idx_stream:
        # Advance the rate stream to this grid; grids with no rates are skipped entirely,
        # since every metric the analyzer shows needs a premium.
        while rates_next is not None and rates_next[0] < grid_id:
            rates_next = next(rate_stream, None)
        if rates_next is None:
            return
        if rates_next[0] != grid_id:
            continue

        matrix: dict[int, dict[str, float]] = {}
        for year, iv, val in idx_rows:
            matrix.setdefault(year, {})[iv] = val

        rate_rows = rates_next[1]
        rate_year = max(r[0] for r in rate_rows)
        rates: dict[float, dict[str, float]] = {}
        offered: dict[str, set[str]] = {}
        for year, use, iv, cov, rate in rate_rows:
            if year != rate_year:
                continue
            offered.setdefault(use, set()).add(iv)
            # One rate table for all three uses — verified identical; see module docstring.
            rates.setdefault(round(cov, 2), {})[iv] = rate

        yield (int(grid_id), YEAR_MIN, YEAR_MAX, int(rate_year),
               pack_index(matrix), pack_means(matrix), pack_rates(rates),
               pack_use_mask(offered), index_source)
