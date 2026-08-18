"""Async client for the pvnode API v2."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import DEFAULT_BASE_URL

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class PvnodeError(Exception):
    """Base error for the pvnode API client."""


class PvnodeAuthError(PvnodeError):
    """The API key was rejected."""


class PvnodeConnectionError(PvnodeError):
    """The pvnode API could not be reached."""


class PvnodePlanError(PvnodeError):
    """The plan does not allow this call (no forecast access, site deactivated)."""


class PvnodeVariabilityUnavailable(PvnodeError):
    """The plan does not include the variability band.

    Not a failure: the caller retries without `include=variability`.
    """


class PvnodeNotFoundError(PvnodeError):
    """The site does not exist (any more)."""


class PvnodeQuotaExceeded(PvnodeError):
    """The monthly request quota is used up."""

    def __init__(self, message: str, reset: str | None = None) -> None:
        """Store the reset timestamp alongside the message."""
        super().__init__(message)
        self.reset = reset


class PvnodeApiError(PvnodeError):
    """Any other non-2xx response."""

    def __init__(self, status: int, message: str) -> None:
        """Store the HTTP status alongside the message."""
        super().__init__(message)
        self.status = status


@dataclass
class RequestLimit:
    """The caller's monthly request quota, from the `RequestLimit-*` response headers.

    Account-wide, not per site: the quota is counted on the user id, so every config
    entry of the same account sees the same numbers.
    """

    limit: str | None = None
    used: int | None = None
    remaining: str | None = None
    reset: str | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> RequestLimit:
        """Build from response headers, tolerating missing or non-numeric values."""

        def _int(value: str | None) -> int | None:
            try:
                return int(value) if value is not None else None
            except ValueError:
                return None

        return cls(
            limit=headers.get("RequestLimit-Limit"),
            used=_int(headers.get("RequestLimit-Used")),
            remaining=headers.get("RequestLimit-Remaining"),
            reset=headers.get("RequestLimit-Reset"),
        )


@dataclass
class ForecastResult:
    """A forecast response plus the quota headers that came with it."""

    payload: dict[str, Any]
    request_limit: RequestLimit = field(default_factory=RequestLimit)


def _classify_403(detail: str) -> PvnodeError:
    """Map a 403 body to the right error.

    pvnode uses 403 for three unrelated situations, and the integration has to react
    differently to each: a missing variability band is recoverable, a plan without
    forecast access is not, and a deactivated site needs its own message.
    """
    lowered = detail.lower()
    if "variability" in lowered:
        return PvnodeVariabilityUnavailable(detail)
    if "inactive" in lowered or "site limit" in lowered:
        return PvnodePlanError(detail)
    if "does not include" in lowered:
        return PvnodePlanError(detail)
    return PvnodeAuthError(detail or "pvnode rejected the API key (HTTP 403)")


class PvnodeApiClient:
    """Thin async HTTP client for the pvnode forecast and sites endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str | None = None,
    ) -> None:
        """Initialize with a shared aiohttp session, API key and base URL.

        `user_agent` identifies the client to pvnode. It rides on a request that
        happens anyway, carries nothing about the user, and can be read from the
        server's normal access logs — no telemetry is sent from here.
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        if user_agent:
            self._headers["User-Agent"] = user_agent

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, RequestLimit]:
        """GET `path` and return the decoded body plus the quota headers."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("GET %s params=%s", url, params)
        try:
            async with self._session.get(
                url, params=params, headers=self._headers, timeout=_TIMEOUT
            ) as response:
                limits = RequestLimit.from_headers(response.headers)

                if response.status >= 400:
                    detail = await self._error_detail(response)
                    raise self._error_for(response.status, detail, limits)

                return await response.json(content_type=None), limits
        except asyncio.TimeoutError as err:
            raise PvnodeConnectionError(
                "Timeout while contacting the pvnode API"
            ) from err
        except aiohttp.ClientError as err:
            raise PvnodeConnectionError(
                f"Error contacting the pvnode API: {err}"
            ) from err

    @staticmethod
    async def _error_detail(response: aiohttp.ClientResponse) -> str:
        """Pull `detail` out of an error body, falling back to raw text."""
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return (await response.text())[:200]
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
        return str(body)[:200]

    @staticmethod
    def _error_for(status: int, detail: str, limits: RequestLimit) -> PvnodeError:
        """Turn an HTTP status plus body into the matching exception."""
        if status == 401:
            return PvnodeAuthError(detail or "pvnode rejected the API key (HTTP 401)")
        if status == 403:
            return _classify_403(detail)
        if status == 404:
            return PvnodeNotFoundError(detail or "Site not found")
        if status == 429:
            return PvnodeQuotaExceeded(
                detail or "Request quota exhausted", limits.reset
            )
        return PvnodeApiError(status, f"HTTP {status}: {detail}")

    async def async_list_sites(self) -> list[dict[str, Any]]:
        """List the account's sites.

        Open on every plan and not counted against the forecast quota, so this is safe
        to call from the config flow.
        """
        data, _ = await self._get("/v2/sites/")
        return data if isinstance(data, list) else []

    async def async_get_site(self, site_id: str) -> dict[str, Any]:
        """Fetch a single site, including its `strings` array."""
        data, _ = await self._get(f"/v2/sites/{site_id}")
        return data

    async def async_get_forecast(
        self, site_id: str, *, variability: bool = False
    ) -> ForecastResult:
        """Fetch the full forecast for a site.

        `forecast_days` is deliberately omitted — the API then returns the plan maximum,
        so the horizon needs no local configuration. Timestamps are requested in UTC:
        the naive site-local default would have to be localized here, which is exactly
        where DST bugs live.
        """
        # Everything except the variability band, which is plan-gated and would turn a
        # missing entitlement into a hard 403. The extra groups only widen the response;
        # they cost no additional request.
        include = ["default", "clearsky", "weather", "irradiance", "strings"]
        if variability:
            include.append("variability")

        payload, limits = await self._get(
            f"/v2/forecast/{site_id}",
            {"include": include, "timezone": "utc", "past_days": 0},
        )
        return ForecastResult(payload=payload, request_limit=limits)
