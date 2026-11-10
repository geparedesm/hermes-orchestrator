"""Validate the small, secret-free contract returned by independent quota probes."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

STATUSES = frozenset({"OK", "AUTH_REQUIRED", "PROFILE_REQUIRED", "UNAVAILABLE", "TIMEOUT", "BUSY", "INVALID_RESPONSE"})


def snapshot(value: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Rebuild, never forward, the allowlisted payload. An unknown field cannot escape the boundary."""
    invalid = {"status": "INVALID_RESPONSE", "windows": []}
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        return invalid
    status = value["status"]
    if status != "OK":
        return {"status": status, "windows": []}
    windows = value.get("windows")
    if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
        return invalid
    now = now or datetime.now(timezone.utc)
    out, seen = [], set()
    for w in windows:
        if not isinstance(w, dict) or w.get("window") not in ("session", "week") or w["window"] in seen:
            return invalid
        used, reset, observed = (w.get(k) for k in ("used_percent", "resets_at", "observed_at"))
        if not all(type(v) in (int, float) and isfinite(v) for v in (used, reset, observed)):
            return invalid
        if not 0 <= used <= 100 or not 0 < observed <= now.timestamp() < reset <= now.timestamp() + 8 * 86400:
            return invalid
        seen.add(w["window"])
        out.append({"window": w["window"], "used_percent": float(used), "resets_at": reset, "observed_at": observed})
    return {"status": "OK", "windows": out}
