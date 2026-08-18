"""Parsing and accessors for a pvnode v2 forecast payload.

Timestamps arrive as RFC 3339 UTC (`?timezone=utc`) and are kept as aware datetimes
throughout — no local-time arithmetic anywhere. Only `daily[].date` is a site-local
calendar date, which is why the site's timezone is needed to resolve "today".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.util import dt as dt_util

from .api import RequestLimit
from .const import SLOT_HOURS

_LOGGER = logging.getLogger(__name__)


def safe_timezone(name: str | None) -> tzinfo:
    """Return the site's timezone, falling back to HA's own on anything unusable."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            _LOGGER.warning("Unknown site timezone %s, falling back to local", name)
    return dt_util.DEFAULT_TIME_ZONE


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an RFC 3339 timestamp into an aware datetime."""
    if not value:
        return None
    return dt_util.parse_datetime(value)


@dataclass
class PvnodeData:
    """One coordinator refresh, parsed.

    Rows are kept as raw dicts rather than typed fields: which keys are present depends
    on the requested `include` groups and the plan, and every sensor only ever reads one
    of them by name.
    """

    values: dict[datetime, dict[str, Any]] = field(default_factory=dict)
    daily: list[dict[str, Any]] = field(default_factory=list)
    strings: dict[int, dict[datetime, dict[str, Any]]] = field(default_factory=dict)

    computed_at: datetime | None = None
    next_poll_at: datetime | None = None
    included: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    request_limit: RequestLimit = field(default_factory=RequestLimit)

    site_timezone: str | None = None
    # Per string index: the placeholders for its translated device name.
    string_names: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def has_variability(self) -> bool:
        """True when this payload actually carries the min/max band."""
        return "variability" in self.included

    @property
    def supports_variability(self) -> bool:
        """True when the plan allows the band, whether or not it was requested.

        `available` states the entitlement rather than the request, so it is the
        signal to decide what to ask for next time.
        """
        return "variability" in self.available

    @property
    def tz(self) -> tzinfo:
        """The site's timezone."""
        return safe_timezone(self.site_timezone)

    @property
    def string_indexes(self) -> list[int]:
        """Sorted string indexes present in this payload."""
        return sorted(self.strings)

    # -- day resolution ----------------------------------------------------

    def local_today(self) -> date:
        """Today's calendar date at the site."""
        return dt_util.utcnow().astimezone(self.tz).date()

    def day(self, offset: int) -> dict[str, Any] | None:
        """Return the `daily` entry `offset` days from today, or None.

        Matched by date rather than by position so a missing day (the API drops days
        with fewer than 80 timesteps) shifts nothing.
        """
        target = (self.local_today() + timedelta(days=offset)).isoformat()
        for entry in self.daily:
            if entry.get("date") == target:
                return entry
        return None

    def day_bounds(self, offset: int) -> tuple[datetime, datetime]:
        """UTC start/end instants of the site-local day `offset` days from today."""
        target = self.local_today() + timedelta(days=offset)
        start = datetime.combine(target, datetime.min.time(), tzinfo=self.tz)
        return start, start + timedelta(days=1)


def parse_forecast(
    payload: dict[str, Any],
    *,
    site_timezone: str | None = None,
    request_limit: RequestLimit | None = None,
    string_names: dict[int, dict[str, str]] | None = None,
) -> PvnodeData:
    """Turn a raw v2 forecast response into a `PvnodeData`."""
    values: dict[datetime, dict[str, Any]] = {}
    for row in payload.get("values") or []:
        if (ts := _parse_ts(row.get("timestamp"))) is not None:
            values[ts] = row

    strings: dict[int, dict[datetime, dict[str, Any]]] = {}
    for row in payload.get("strings") or []:
        index = row.get("string_index")
        ts = _parse_ts(row.get("timestamp"))
        if index is None or ts is None:
            continue
        strings.setdefault(int(index), {})[ts] = row

    return PvnodeData(
        values=values,
        daily=list(payload.get("daily") or []),
        strings=strings,
        computed_at=_parse_ts(payload.get("computed_at")),
        next_poll_at=_parse_ts(payload.get("next_poll_at")),
        included=list(payload.get("included") or []),
        available=list(payload.get("available") or []),
        request_limit=request_limit or RequestLimit(),
        # With `?timezone=utc` the payload's own `timezone` field reads "UTC", so the
        # site's real zone has to come from the config entry.
        site_timezone=site_timezone,
        string_names=string_names or {},
    )


# ---------------------------------------------------------------------------
# Series accessors — shared by the site and per-string sensors
# ---------------------------------------------------------------------------


def value_at(
    series: dict[datetime, dict[str, Any]], key: str, when: datetime
) -> float | None:
    """Value of `key` in the slot covering `when` (the latest slot at or before it)."""
    candidates = [ts for ts in series if ts <= when]
    if not candidates:
        return None
    return _number(series[max(candidates)].get(key))


def value_now(series: dict[datetime, dict[str, Any]], key: str) -> float | None:
    """Value of `key` in the current slot."""
    return value_at(series, key, dt_util.utcnow())


def energy_between(
    series: dict[datetime, dict[str, Any]],
    start: datetime,
    end: datetime,
    key: str = "pv_power",
) -> float | None:
    """Wh accumulated over [start, end) — power in W across a 15-minute grid."""
    total = 0.0
    found = False
    for ts, row in series.items():
        if start <= ts < end and (power := _number(row.get(key))) is not None:
            total += power * SLOT_HOURS
            found = True
    return total if found else None


def peak(
    series: dict[datetime, dict[str, Any]],
    start: datetime,
    end: datetime,
    key: str = "pv_power",
) -> tuple[datetime, float] | None:
    """Highest value of `key` in [start, end) and when it occurs."""
    best: tuple[datetime, float] | None = None
    for ts, row in series.items():
        if not start <= ts < end:
            continue
        if (value := _number(row.get(key))) is None:
            continue
        if best is None or value > best[1]:
            best = (ts, value)
    return best


def series_entries(
    series: dict[datetime, dict[str, Any]], key: str, name: str
) -> list[dict[str, Any]]:
    """The full time series for one metric, for the `forecast` state attribute.

    One small attribute per entity rather than one combined blob, so a single state
    payload stays small no matter how many days the plan allows.
    """
    return [
        {"datetime": ts.isoformat(), name: value}
        for ts, row in sorted(series.items())
        if (value := _number(row.get(key))) is not None
    ]


def _number(value: Any) -> float | None:
    """Coerce an API value to float, or None when it is absent or unusable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
