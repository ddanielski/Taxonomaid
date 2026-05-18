"""Unit tests for the health-probe helpers."""

from __future__ import annotations

import httpx
import pytest

from taxonomaid.services.health import ProbeStatus, probe_llm, probe_telegram

pytestmark = pytest.mark.unit


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_probe_llm_ok_on_200() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = _client(httpx.MockTransport(handle))
    result = await probe_llm(
        base_url="https://example.invalid/v1",
        api_key="x",
        client=client,
    )
    assert result.status is ProbeStatus.OK


async def test_probe_llm_ok_on_404_for_self_hosted() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(httpx.MockTransport(handle))
    result = await probe_llm(
        base_url="http://localhost:11434/v1",
        api_key="",
        client=client,
    )
    assert result.status is ProbeStatus.OK
    assert "self-hosted" in result.detail


async def test_probe_llm_degraded_on_401() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = _client(httpx.MockTransport(handle))
    result = await probe_llm(
        base_url="https://example.invalid/v1",
        api_key="bad",
        client=client,
    )
    assert result.status is ProbeStatus.DEGRADED


async def test_probe_llm_failed_on_connection_error() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    client = _client(httpx.MockTransport(handle))
    result = await probe_llm(
        base_url="https://example.invalid/v1",
        api_key="x",
        client=client,
    )
    assert result.status is ProbeStatus.FAILED
    assert "unreachable" in result.detail


async def test_probe_telegram_ok_on_get_me() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "bot"}})

    client = _client(httpx.MockTransport(handle))
    result = await probe_telegram(bot_token="123:abc", client=client)
    assert result.status is ProbeStatus.OK


async def test_probe_telegram_degraded_on_401() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    client = _client(httpx.MockTransport(handle))
    result = await probe_telegram(bot_token="bad", client=client)
    assert result.status is ProbeStatus.DEGRADED


async def test_probe_telegram_failed_on_connection_error() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    client = _client(httpx.MockTransport(handle))
    result = await probe_telegram(bot_token="x", client=client)
    assert result.status is ProbeStatus.FAILED
