"""Exercise the trusted proxy boundary without Docker, credentials or model calls."""

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from studynavigator import __main__ as entrypoint


async def echo_scope(scope, receive, send):
    import json

    body = json.dumps({"client": scope["client"][0], "scheme": scope["scheme"]}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.mark.asyncio
@pytest.mark.parametrize("peer,expected_client,expected_scheme", [
    ("172.30.43.2", "198.51.100.25", "https"),
    ("172.30.43.3", "172.30.43.3", "http"),
    ("198.51.100.50", "198.51.100.50", "http"),
])
async def test_only_caddy_peer_can_supply_forwarded_client_and_scheme(peer, expected_client, expected_scheme):
    app = ProxyHeadersMiddleware(echo_scope, trusted_hosts="172.30.43.2")
    transport = httpx.ASGITransport(app=app, client=(peer, 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
        response = await client.get("/", headers={"X-Forwarded-For": "198.51.100.25",
                                                   "X-Forwarded-Proto": "https"})
    assert response.json() == {"client": expected_client, "scheme": expected_scheme}


def test_entrypoint_keeps_single_worker_and_explicit_proxy_trust(monkeypatch):
    called = {}
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.30.43.2")
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: called.update(app=app, **kwargs))
    entrypoint.main()
    assert called == {"app": "studynavigator.main:app", "host": "0.0.0.0", "port": 8000,
                      "workers": 1, "proxy_headers": True, "forwarded_allow_ips": "172.30.43.2"}
