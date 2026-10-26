# COPIED VERBATIM from aip_products/src/webapp/auth.py — edit it there; tests/test_web.py checks it matches.
"""Passcode gate helpers.

The actual passcode never lives in source. `allowed_passcodes()` reads it from
Streamlit secrets (`app_passcode`, or a `[passcodes]` table of named users) with
a fallback to the `APP_PASSCODE` environment variable. `verify_passcode()` is a
pure, constant-time comparison so it can be unit-tested without a running app.
"""
from __future__ import annotations

import hmac
import os
from typing import Iterable


def verify_passcode(candidate: str, allowed: Iterable[str]) -> bool:
    """True iff `candidate` matches any allowed passcode (constant-time).

    Uses hmac.compare_digest against every allowed value (no short-circuit on
    the first char) so the check does not leak length/content via timing. An
    empty candidate or an empty allow-list never authenticates.
    """
    if not candidate:
        return False
    ok = False
    for value in allowed:
        if not value:
            continue
        # OR-accumulate; compare_digest is constant-time per comparison.
        if hmac.compare_digest(str(candidate), str(value)):
            ok = True
    return ok


def allowed_passcodes(secrets: dict | None = None) -> list[str]:
    """Collect every configured passcode.

    Sources, in order: `app_passcode` (single) and every value under a
    `[passcodes]` table in Streamlit secrets, plus the `APP_PASSCODE`
    environment variable. `secrets` is injected for testability; in the app it
    is `st.secrets`. Never returns the values in source — they come from
    deploy-time configuration only.
    """
    out: list[str] = []
    if secrets:
        single = secrets.get("app_passcode")
        if single:
            out.append(str(single))
        table = secrets.get("passcodes")
        if table:
            try:
                out.extend(str(v) for v in dict(table).values() if v)
            except (TypeError, ValueError):
                pass
    env = os.environ.get("APP_PASSCODE")
    if env:
        out.append(env)
    # De-dupe while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq
