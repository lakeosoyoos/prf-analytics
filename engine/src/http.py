"""Polite HTTP with a SQLite-backed URL cache.

One requests.Session per Client, a configurable delay between hits to the same host, retry/backoff
on transient errors, and a url_cache table so a refresh re-downloads a file only when it has changed
(or when force=True). Downloads land in data/cache/ for provenance.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify_url(url: str) -> str:
    p = urlparse(url)
    tail = (p.path.rstrip("/").rsplit("/", 1)[-1] or "index")
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in tail)[:60]
    return f"{p.netloc.replace(':', '_')}__{safe}__{h}"


class Client:
    def __init__(self, cfg: config.Config, conn):
        self.cfg = cfg
        self.conn = conn
        self._last_hit: dict[str, float] = {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.user_agent
        retry = Retry(
            total=cfg.max_retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- politeness -------------------------------------------------------
    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.cfg.request_delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # -- cache bookkeeping ------------------------------------------------
    def _cache_row(self, url: str):
        return self.conn.execute("SELECT * FROM url_cache WHERE url = ?", (url,)).fetchone()

    def _is_fresh(self, row) -> bool:
        if not row or not row["fetched_at"] or not row["local_path"]:
            return False
        if not Path(row["local_path"]).exists():
            return False
        if self.cfg.cache_max_age_days <= 0:
            return True
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return False
        return datetime.now(timezone.utc) - fetched < timedelta(days=self.cfg.cache_max_age_days)

    def _record_cache(self, url, path, resp, sha):
        self.conn.execute(
            """INSERT INTO url_cache (url, local_path, etag, last_modified, sha256, http_status, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 local_path=excluded.local_path, etag=excluded.etag,
                 last_modified=excluded.last_modified, sha256=excluded.sha256,
                 http_status=excluded.http_status, fetched_at=excluded.fetched_at""",
            (url, str(path), resp.headers.get("ETag"), resp.headers.get("Last-Modified"),
             sha, resp.status_code, _now_iso()),
        )
        self.conn.commit()

    # -- public API -------------------------------------------------------
    def get(self, url: str, *, headers=None, params=None) -> requests.Response:
        """Plain GET (no file cache) — for small JSON APIs."""
        self._throttle(url)
        return self.session.get(
            url, headers=headers, params=params, timeout=self.cfg.timeout_seconds
        )

    def get_json(self, url: str, *, headers=None, params=None):
        r = self.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    def download(self, url: str, *, force: bool = False, headers=None) -> Path:
        """Download to data/cache/, honoring the url_cache freshness gate. Returns local path."""
        row = self._cache_row(url)
        if not force and self._is_fresh(row):
            return Path(row["local_path"])

        self._throttle(url)
        # Conditional GET when we have validators and aren't forcing.
        req_headers = dict(headers or {})
        if row and not force:
            if row["etag"]:
                req_headers["If-None-Match"] = row["etag"]
            if row["last_modified"]:
                req_headers["If-Modified-Since"] = row["last_modified"]

        resp = self.session.get(
            url, headers=req_headers, timeout=self.cfg.timeout_seconds, stream=True
        )
        if resp.status_code == 304 and row and Path(row["local_path"]).exists():
            self.conn.execute(
                "UPDATE url_cache SET fetched_at = ? WHERE url = ?", (_now_iso(), url)
            )
            self.conn.commit()
            return Path(row["local_path"])
        resp.raise_for_status()

        path = config.CACHE_DIR / _slugify_url(url)
        hasher = hashlib.sha256()
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    hasher.update(chunk)
        self._record_cache(url, path, resp, hasher.hexdigest())
        return path
