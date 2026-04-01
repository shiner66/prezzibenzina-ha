#!/usr/bin/env python3
"""Standalone probe script for the prezzibenzina.it API.

Run with:
    python3 scripts/probe_pb_api.py [lat] [lon] [radius_km]

Defaults to Rome centre (41.9028, 12.4964, 10 km).

Requirements: pip install aiohttp
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Config — override via CLI args or env
# ---------------------------------------------------------------------------
LAT = float(sys.argv[1]) if len(sys.argv) > 1 else 41.9028
LON = float(sys.argv[2]) if len(sys.argv) > 2 else 12.4964
RADIUS = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0

BASE = "https://api3.prezzibenzina.it/"
UDID = str(uuid.uuid4())
PBID = str(uuid.uuid4())

CLIENT_PARAMS: dict[str, Any] = {
    "platform": "android",
    "pbid": PBID,
    "udid": UDID,
    "appversion": "5.0.0",
    "sdk": "33",
}

TIMEOUT = aiohttp.ClientTimeout(total=20)

# Colour helpers
_G = "\033[32m"  # green
_R = "\033[31m"  # red
_Y = "\033[33m"  # yellow
_B = "\033[1m"   # bold
_E = "\033[0m"   # reset


def _ok(msg: str) -> None:
    print(f"{_G}✓{_E} {msg}")


def _err(msg: str) -> None:
    print(f"{_R}✗{_E} {msg}")


def _info(msg: str) -> None:
    print(f"{_Y}→{_E} {msg}")


def _header(msg: str) -> None:
    print(f"\n{_B}{'─'*60}")
    print(f"  {msg}")
    print(f"{'─'*60}{_E}")


def _dump(data: Any, max_items: int = 3) -> None:
    """Pretty-print a JSON-like object, truncating large lists."""
    if isinstance(data, dict):
        truncated = {}
        for k, v in data.items():
            if isinstance(v, list) and len(v) > max_items:
                truncated[k] = v[:max_items] + [f"... ({len(v) - max_items} more)"]
            else:
                truncated[k] = v
        print(json.dumps(truncated, indent=2, ensure_ascii=False, default=str))
    elif isinstance(data, list):
        subset = data[:max_items]
        if len(data) > max_items:
            subset.append(f"... ({len(data) - max_items} more)")
        print(json.dumps(subset, indent=2, ensure_ascii=False, default=str))
    else:
        print(repr(data))


# ---------------------------------------------------------------------------
# Core probe helpers
# ---------------------------------------------------------------------------

async def probe_endpoint(
    session: aiohttp.ClientSession,
    endpoint: str,
    params: dict[str, Any],
    method: str = "GET",
) -> tuple[int, Any]:
    """Hit one endpoint and return (status_code, parsed_json_or_text).

    L'API usa il pattern RPC-over-HTTP: l'azione va nel parametro "do",
    non nel path URL. Es: GET /?do=pb_get_stations&lat=...
    """
    url = BASE.rstrip("/") + "/"
    try:
        if method == "GET":
            async with session.get(url, params={"do": endpoint, **params}, timeout=TIMEOUT) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
        else:  # POST form
            async with session.post(url, data={"do": endpoint, **params}, timeout=TIMEOUT) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
        return status, body
    except aiohttp.ClientConnectorError as exc:
        return -1, f"Connection error: {exc}"
    except asyncio.TimeoutError:
        return -2, "Timeout"
    except Exception as exc:  # noqa: BLE001
        return -3, f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Step 1 — session key
# ---------------------------------------------------------------------------

async def step_session_key(session: aiohttp.ClientSession) -> str | None:
    _header("STEP 1 — pb_get_session_key")
    params = {**CLIENT_PARAMS}

    for method in ("GET", "POST"):
        _info(f"Trying {method} pb_get_session_key ...")
        status, body = await probe_endpoint(session, "pb_get_session_key", params, method)
        print(f"  HTTP {status}")
        if status in (200, 201):
            _ok(f"Got response ({method})")
            _dump(body)
            # Extract token
            token = _extract_token(body)
            if token:
                _ok(f"Token found: {token[:20]}...")
                return token
            else:
                _err("Could not extract token from response")
        else:
            _err(f"HTTP {status}: {str(body)[:200]}")

    return None


def _extract_token(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("session_key", "token", "sessiontoken", "key", "data"):
        val = raw.get(key)
        if isinstance(val, str) and len(val) > 4:
            return val
        if isinstance(val, dict):
            for subkey in ("session_key", "token", "key"):
                subval = val.get(subkey)
                if isinstance(subval, str) and len(subval) > 4:
                    return subval
    return None


# ---------------------------------------------------------------------------
# Step 2 — create session
# ---------------------------------------------------------------------------

async def step_create_session(
    session: aiohttp.ClientSession, token: str
) -> None:
    _header("STEP 2 — pb_create_session")
    params = {**CLIENT_PARAMS, "token": token, "session_key": token}
    for method in ("GET", "POST"):
        _info(f"Trying {method} pb_create_session ...")
        status, body = await probe_endpoint(session, "pb_create_session", params, method)
        print(f"  HTTP {status}")
        if status in (200, 201):
            _ok(f"Session created ({method})")
            _dump(body)
            return
        else:
            _err(f"HTTP {status}: {str(body)[:200]}")


# ---------------------------------------------------------------------------
# Step 3 — pb_get_stations (core probe, multiple param variants)
# ---------------------------------------------------------------------------

async def step_get_stations(
    session: aiohttp.ClientSession,
    token: str | None,
) -> None:
    _header("STEP 3 — pb_get_stations")

    base_params: dict[str, Any] = {
        **CLIENT_PARAMS,
        "lat": LAT,
        "lng": LON,
        "latitude": LAT,
        "longitude": LON,
    }
    if token:
        base_params["session_key"] = token
        base_params["token"] = token

    variants = [
        ("minimal — lat/lng only", {"lat": LAT, "lng": LON, **CLIENT_PARAMS}),
        ("full params + radius",   {**base_params, "radius": RADIUS, "raggio": RADIUS}),
        ("full params + fuel",     {**base_params, "fuel": "diesel"}),
        ("full params + fuel benzina", {**base_params, "fuel": "benzina"}),
    ]

    for label, params in variants:
        for method in ("GET", "POST"):
            _info(f"Variant '{label}' — {method}")
            status, body = await probe_endpoint(session, "pb_get_stations", params, method)
            print(f"  HTTP {status}")
            if status in (200, 201):
                if isinstance(body, (dict, list)):
                    _ok(f"JSON response ({method})")
                    _dump(body)
                    _analyse_stations_response(body)
                    return  # found a working combo — stop
                else:
                    _info(f"Text response: {str(body)[:300]}")
            else:
                _err(f"HTTP {status}: {str(body)[:200]}")


def _analyse_stations_response(body: Any) -> None:
    """Try to interpret what the response contains."""
    if not isinstance(body, dict):
        _info("Response is not a dict — can't auto-analyse")
        return

    print(f"\n  Top-level keys: {list(body.keys())}")

    for key in ("stations", "data", "distributori", "results", "items"):
        candidate = body.get(key)
        if isinstance(candidate, list):
            _ok(f"Found station list under key '{key}' with {len(candidate)} items")
            if candidate:
                first = candidate[0]
                if isinstance(first, dict):
                    print(f"  First item keys: {list(first.keys())}")
                    if "prices" in first:
                        prices = first["prices"]
                        print(f"  Prices field: {prices[:2] if isinstance(prices, list) else prices}")
            return

    _info("No recognisable station list found — check top-level keys above")


# ---------------------------------------------------------------------------
# Step 4 — pb_get_prices (per-station detail)
# ---------------------------------------------------------------------------

async def step_get_prices(
    session: aiohttp.ClientSession,
    token: str | None,
) -> None:
    _header("STEP 4 — pb_get_prices (station detail, dummy stationID=1)")
    params: dict[str, Any] = {
        **CLIENT_PARAMS,
        "stationID": 1,
        "stationId": 1,
    }
    if token:
        params["session_key"] = token

    for method in ("GET", "POST"):
        _info(f"Trying {method} pb_get_prices ...")
        status, body = await probe_endpoint(session, "pb_get_prices", params, method)
        print(f"  HTTP {status}")
        if status in (200, 201):
            _ok(f"Response ({method})")
            _dump(body)
            return
        else:
            _err(f"HTTP {status}: {str(body)[:200]}")


# ---------------------------------------------------------------------------
# Step 5 — pb_get_brands
# ---------------------------------------------------------------------------

async def step_get_brands(
    session: aiohttp.ClientSession,
    token: str | None,
) -> None:
    _header("STEP 5 — pb_get_brands")
    params = {**CLIENT_PARAMS}
    if token:
        params["session_key"] = token

    for method in ("GET", "POST"):
        _info(f"Trying {method} pb_get_brands ...")
        status, body = await probe_endpoint(session, "pb_get_brands", params, method)
        print(f"  HTTP {status}")
        if status in (200, 201):
            _ok(f"Response ({method})")
            _dump(body)
            return
        else:
            _err(f"HTTP {status}: {str(body)[:200]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{_B}Prezzibenzina.it API probe{_E}")
    print(f"  Base URL : {BASE}")
    print(f"  Location : lat={LAT}, lon={LON}, radius={RADIUS} km")
    print(f"  UDID     : {UDID[:16]}...")

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Session flow
        token = await step_session_key(session)
        if token:
            await step_create_session(session, token)
        else:
            _info("Proceeding without session token")

        # Core endpoints
        await step_get_stations(session, token)
        await step_get_prices(session, token)
        await step_get_brands(session, token)

    _header("SUMMARY")
    if token:
        _ok("Session flow: SUCCEEDED")
    else:
        _err("Session flow: FAILED (no token — check if pb_get_session_key is reachable)")

    print("\nNext steps:")
    print("  1. If pb_get_stations returned JSON with a station list → PB client will work")
    print("  2. If HTTP 401/403 → session is mandatory; check token extraction logic")
    print("  3. If HTTP 400 'Invalid parameters' → adjust param names in pb_api.py")
    print("  4. If connection error → api3.prezzibenzina.it may be IP-restricted or down")
    print()


if __name__ == "__main__":
    asyncio.run(main())
