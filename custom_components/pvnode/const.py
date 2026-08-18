"""Constants for the pvnode integration."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "pvnode"
MANUFACTURER: Final = "pvnode"

PLATFORMS: Final = [Platform.BUTTON, Platform.SENSOR]

# Config entry data
CONF_SITE_ID: Final = "site_id"
CONF_TIMEZONE: Final = "timezone"

# Local development override. There is no UI for this on purpose: a server field in the
# setup dialog is noise for every real user and a support burden when someone edits it by
# accident. Set PVNODE_BASE_URL in the environment before Home Assistant starts instead.
DEFAULT_BASE_URL: Final = (
    os.environ.get("PVNODE_BASE_URL", "").strip().rstrip("/") or "https://api.pvnode.com"
)

# The API advertises its own poll cadence via `next_poll_at`. Any positive delay is
# taken as-is — a short one just means the next slot is close, which is normal right
# after a restore. The floor only exists so a server returning "now" over and over
# cannot spin into a request loop.
MIN_UPDATE_INTERVAL: Final = timedelta(minutes=1)
MAX_UPDATE_INTERVAL: Final = timedelta(hours=24)
FALLBACK_UPDATE_INTERVAL: Final = timedelta(hours=1)

# Polling exactly at the advertised boundary risks racing the server's clock and being
# served the slot we already have — which still costs a request. Land just after it.
POLL_GRACE: Final = timedelta(seconds=30)

# Cached responses count against the monthly quota, so once it is exhausted there is
# nothing to gain from retrying on the normal cadence.
QUOTA_BACKOFF: Final = timedelta(hours=1)

# Sensors walk the stored curve locally; this never triggers an API call.
LOCAL_REFRESH_INTERVAL: Final = timedelta(minutes=5)

STORAGE_VERSION: Final = 1

ISSUE_QUOTA_EXHAUSTED: Final = "quota_exhausted"

# Forecasts are computed on a 15-minute grid
SLOT_MINUTES: Final = 15
SLOT_HOURS: Final = SLOT_MINUTES / 60

SITE_KEY: Final = "site"
