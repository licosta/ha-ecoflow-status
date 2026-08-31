"""Async HTTP client for the EcoFlow IoT Open Platform."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from typing import Any

import aiohttp

from .const import (
    PATH_DEVICE_LIST,
    PATH_DEVICE_QUOTA_ALL,
    REGIONS,
    REGION_EU,
    REGION_GLOBAL,
    REGION_NA,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20  # seconds


class EcoFlowAPIError(Exception):
    """Raised when the EcoFlow API returns an error or is unreachable."""


class EcoFlowAuthError(EcoFlowAPIError):
    """Raised on authentication failures (bad key, bad signature, etc.)."""


class EcoFlowClient:
    """Thin async wrapper around the EcoFlow IoT Open Platform HTTP API.

    Each request is signed with HMAC-SHA256 over the alphabetically sorted
    request parameters concatenated with `accessKey`, `nonce` and `timestamp`,
    keyed by the `secretKey`. See:
    https://developer-eu.ecoflow.com (developer portal, region-aware).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_key: str,
        secret_key: str,
        region: str = REGION_EU,
    ) -> None:
        if region not in REGIONS:
            raise ValueError(f"Unknown region '{region}', expected one of {list(REGIONS)}")
        self._session = session
        self._access_key = access_key.strip()
        self._secret_key = secret_key.strip()
        self._base_url = REGIONS[region]

    # ------------------------------------------------------------------ signing

    def _flatten(self, params: Any, parent_key: str = "") -> list[tuple[str, str]]:
        """Flatten nested dicts/lists into dotted keys with stringified values.

        Example: {"a": {"b": 1}} -> [("a.b", "1")].
        Booleans are lowercased, None becomes "" (EcoFlow convention).
        """
        items: list[tuple[str, str]] = []
        if isinstance(params, dict):
            for k, v in params.items():
                new_key = f"{parent_key}.{k}" if parent_key else str(k)
                if isinstance(v, (dict, list)):
                    items.extend(self._flatten(v, new_key))
                else:
                    if isinstance(v, bool):
                        v = str(v).lower()
                    elif v is None:
                        v = ""
                    items.append((new_key, str(v)))
        elif isinstance(params, list):
            for i, v in enumerate(params):
                new_key = f"{parent_key}[{i}]"
                if isinstance(v, (dict, list)):
                    items.extend(self._flatten(v, new_key))
                else:
                    if isinstance(v, bool):
                        v = str(v).lower()
                    elif v is None:
                        v = ""
                    items.append((new_key, str(v)))
        return items

    def _sign(self, params: dict[str, Any]) -> dict[str, str]:
        """Return the HTTP headers required to authenticate one request."""
        # 5-6 digit nonce (matches tolwi/hassio-ecoflow-cloud; 6 strictly also works
        # but the wider range is what the working reference uses).
        nonce = str(random.randint(10000, 1000000))
        timestamp = str(int(time.time() * 1000))  # ms
        flat_items = self._flatten(params)
        flat_items.sort()  # sort by key (lexicographic)
        qs = "&".join(f"{k}={v}" for k, v in flat_items)
        if qs:
            qs += "&"
        plain = f"{qs}accessKey={self._access_key}&nonce={nonce}&timestamp={timestamp}"
        sign = hmac.new(
            self._secret_key.encode("utf-8"),
            plain.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        # NOTE: do NOT add Content-Type here. The EcoFlow API only needs accessKey,
        # nonce, timestamp, sign headers (matches the working tolwi reference). Adding
        # Content-Type can interact with proxies/ingresses in unexpected ways.
        return {
            "accessKey": self._access_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }

    # ------------------------------------------------------------------ transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a signed HTTP request and return the parsed JSON response.

        For signing, GET requests sign the query string and POST requests sign
        the JSON body — the result is sent in the same `sign` header.

        The URL is built explicitly (rather than letting aiohttp append the
        query string from `params=...`) so that what we sign is exactly what
        gets sent. This matches the working tolwi reference and avoids any
        aiohttp URL-encoding edge cases.
        """
        sign_payload = body if method == "POST" else (params or {})
        headers = self._sign(sign_payload)
        url = f"{self._base_url}{path}"
        if method == "GET" and params:
            # Build query string from the same sorted flat dict we signed.
            # NOTE: _flatten returns list[tuple], not dict, so we sort the list directly.
            flat_items = sorted(self._flatten(params))
            qs = "&".join(f"{k}={v}" for k, v in flat_items)
            url = f"{url}?{qs}"
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        }
        try:
            if method == "GET":
                async with self._session.get(url, **kwargs) as resp:
                    text = await resp.text()
            elif method == "POST":
                async with self._session.post(url, json=body, **kwargs) as resp:
                    text = await resp.text()
            else:
                raise EcoFlowAPIError(f"Unsupported method: {method}")
        except aiohttp.ClientError as err:
            raise EcoFlowAPIError(f"Network error talking to EcoFlow: {err}") from err
        except asyncio.TimeoutError as err:
            raise EcoFlowAPIError("EcoFlow request timed out") from err

        import json as _json

        try:
            payload = _json.loads(text) if text else {}
        except ValueError as err:
            raise EcoFlowAPIError(f"Non-JSON response from EcoFlow: {text[:200]!r}") from err

        if not isinstance(payload, dict):
            raise EcoFlowAPIError(f"Unexpected response shape: {payload!r}")
        code = str(payload.get("code", ""))
        # 8521: signature is wrong (server saw a different signature than what we sent;
        # usually means wrong region, bad keys, clock drift, or URL/query mismatch).
        if code in ("7000", "7001", "7002", "7003", "8521"):
            raise EcoFlowAuthError(
                f"EcoFlow authentication failed: {payload.get('message')} (code={code})"
            )
        if code == "0":
            return payload.get("data") or {}
        raise EcoFlowAPIError(
            f"EcoFlow API error: {payload.get('message')} (code={code})"
        )

    # ------------------------------------------------------------------ public API

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return the list of devices linked to this developer account.

        Each entry has at least: sn, deviceName, productName, productType, status.
        """
        data = await self._request("GET", PATH_DEVICE_LIST)
        return list(data) if isinstance(data, list) else []

    async def get_all_quota(self, sn: str) -> dict[str, Any]:
        """Return the full flat quota dict for a single device.

        Keys are dotted (e.g. `bmsBmsStatus.chargePower`). Values are
        numeric (power in W for the Stream series) or strings (status flags).
        """
        if not sn:
            raise EcoFlowAPIError("Empty device serial number")
        data = await self._request(
            "GET", PATH_DEVICE_QUOTA_ALL, params={"sn": sn}
        )
        if not isinstance(data, dict):
            raise EcoFlowAPIError(f"Unexpected quota payload for {sn}: {data!r}")
        return data
