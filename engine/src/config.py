"""Read config.ini and expose typed settings.

Kept deliberately small (morgan_septic style): a thin wrapper over configparser so the rest of
the code asks for `cfg.reinsurance_year` instead of poking at raw strings.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

# Project root = parent of src/. Everything (data/, output/, config.ini) hangs off here.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.ini"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = ROOT / "output"
SEED_DIR = DATA_DIR / "seed"
DB_PATH = DATA_DIR / "catalog.db"


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    reinsurance_year: int
    serff_states: list[str]
    user_agent: str
    request_delay_seconds: float
    timeout_seconds: int
    max_retries: int
    cache_max_age_days: int
    sources: dict[str, bool]
    rowcrops_extra: list[str]
    rma_adm_base_url: str
    rma_sob_base_url: str
    serff_toi_code: str
    serff_user_agent: str
    # First crop year to load from each Summary-of-Business file family. RMA posts the
    # coverage-level file (sobcov) from 1989 and the unit-structure file (sobtpu) from 1999;
    # raise these to load less history.
    rma_sob_sobcov_start_year: int = 1989
    rma_sob_sobtpu_start_year: int = 1999
    _raw: configparser.ConfigParser = field(repr=False, default=None)

    def source_enabled(self, name: str) -> bool:
        return self.sources.get(name, False)


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in {"on", "true", "1", "yes", "y"}


def load(path: Path | str | None = None) -> Config:
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(path or CONFIG_PATH)

    g = parser["general"]
    h = parser["http"]
    sources = {k: _as_bool(v) for k, v in parser["sources"].items()}

    adm = parser["rma_adm"] if parser.has_section("rma_adm") else {}
    sob = parser["rma_sob"] if parser.has_section("rma_sob") else {}
    serff = parser["serff"] if parser.has_section("serff") else {}

    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)

    return Config(
        reinsurance_year=int(g.get("reinsurance_year", "2026")),
        serff_states=_split_csv(g.get("serff_states", "")),
        user_agent=h.get("user_agent", "aip-products-catalog/0.1"),
        request_delay_seconds=float(h.get("request_delay_seconds", "1.5")),
        timeout_seconds=int(h.get("timeout_seconds", "60")),
        max_retries=int(h.get("max_retries", "3")),
        cache_max_age_days=int(h.get("cache_max_age_days", "7")),
        sources=sources,
        rowcrops_extra=_split_csv(parser.get("rowcrops", "extra", fallback="")),
        rma_adm_base_url=(adm.get("base_url") if adm else "")
        or "https://pubfs-rma.fpac.usda.gov/pub/References/actuarial_data_master/{year}/",
        rma_sob_base_url=(sob.get("base_url") if sob else "")
        or "https://pubfs-rma.fpac.usda.gov/pub/Web_Data_Files/Summary_of_Business/"
        "state_county_crop/",
        serff_toi_code=(serff.get("toi_code") if serff else "") or "02.1000",
        serff_user_agent=(serff.get("browser_user_agent") if serff else "")
        or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        rma_sob_sobcov_start_year=int((sob.get("sobcov_start_year") if sob else None) or 1989),
        rma_sob_sobtpu_start_year=int((sob.get("sobtpu_start_year") if sob else None) or 1999),
        _raw=parser,
    )
