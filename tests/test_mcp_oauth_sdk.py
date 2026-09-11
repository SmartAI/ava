"""Small protocol fixtures for Google-specific SDK compatibility; no account access."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from ava.tool.mcp import MCPServers, ServerConfig
from ava.tool.mcp_oauth import GOOGLE_ISSUER, GOOGLE_READONLY, OAuthConfig


@pytest.mark.parametrize("mcp_host", ["gmailmcp.googleapis.com", "other.example"])
async def test_google_discovery_pkce_and_offline_access(home, monkeypatch, mcp_host):
    # These discovery fields reproduce the public Google endpoint responses, including
    # the root-issuer slash mismatch and the broader set of advertised mailbox scopes.
    url = f"https://{mcp_host}/mcp/v1"
    requests = []
    authorization = {}

    async def peer(request):
        requests.append(request)
        if "/.well-known/oauth-protected-resource" in request.url.path:
            document = {"resource": f"https://{mcp_host}/mcp", "authorization_servers": [GOOGLE_ISSUER + "/"],
                        "scopes_supported": ["https://mail.google.com/", GOOGLE_READONLY]}
            return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(json.dumps(document).encode()))
        if str(request.url) == GOOGLE_ISSUER + "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": GOOGLE_ISSUER,
                "authorization_endpoint": GOOGLE_ISSUER + "/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token", "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "authorization_response_iss_parameter_supported": True})
        if str(request.url) == "https://oauth2.googleapis.com/token":
            params = parse_qs(request.content.decode())
            assert params["client_id"] == ["fixture-client"]
            assert params["client_secret"] == ["fixture-secret"]
            assert params["resource"] == [f"https://{mcp_host}/mcp"]
            if params["grant_type"] == ["authorization_code"]:
                challenge = base64.urlsafe_b64encode(hashlib.sha256(params["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
                assert authorization["code_challenge"] == [challenge]
            return httpx2.Response(200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                                            "token_type": "Bearer", "expires_in": 60, "scope": GOOGLE_READONLY})
        if str(request.url) == url:
            assert request.headers["Authorization"] == "Bearer fixture-access"
            return httpx2.Response(200, json={})
        raise AssertionError(f"Unexpected OAuth endpoint: {request.url}")

    original = httpx2.AsyncClient
    transport = httpx2.MockTransport(peer)
    monkeypatch.setattr("ava.tool.mcp_oauth.httpx2.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    servers = MCPServers(home)
    identity = servers.save(ServerConfig(name="Google fixture", transport="http", url=url,
        oauth=OAuthConfig(client_id="fixture-client", client_secret="fixture-secret", issuer=GOOGLE_ISSUER)))
    auth = await servers.oauth_for(servers.configs()[0])
    assert auth is not None
    try:
        if mcp_host != "gmailmcp.googleapis.com":
            with pytest.raises(ValueError, match="identity validation failed"):
                await auth.start()
            assert all(request.url.host == mcp_host for request in requests), "Do not relax issuer checks for other servers"
            return
        start = await auth.start()
        parsed = urlsplit(start["authorization_url"])
        authorization.update(parse_qs(parsed.query))
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_ISSUER + "/o/oauth2/v2/auth"
        assert authorization["scope"] == [GOOGLE_READONLY]
        assert authorization["access_type"] == ["offline"] and authorization["prompt"] == ["consent"]
        assert authorization["code_challenge_method"] == ["S256"]
        assert all(request.url.path != "/token" for request in requests), "No exchange before browser consent"
        await auth.complete(start["flow_id"], code="fixture-code", state=authorization["state"][0], iss=GOOGLE_ISSUER)
        assert auth.status == "authorized"
        auth.data["expires_at"] = time.time() - 1
        await auth._persist()
        await servers.aclose()
        servers = MCPServers(home)
        auth = await servers.oauth_for(servers.configs()[0])
        async with original(transport=transport, auth=auth) as client:
            assert (await client.post(url, headers={"MCP-Protocol-Version": "2026-07-28"})).status_code == 200
        assert len([r for r in requests if r.url.path == "/token"]) == 2
        assert servers.configs()[0]["id"] == identity
    finally:
        await servers.aclose()


async def test_login_timeout_and_untrusted_token_response_do_not_leak_secrets(home, monkeypatch, caplog):
    from tests.fixtures.oauth_server import OAuthServer

    peer = OAuthServer()
    servers = MCPServers(home)
    servers.save(ServerConfig(name="Expiry fixture", transport="http", url=peer.url,
        oauth=OAuthConfig(client_id=peer.client_id, client_secret=peer.client_secret, issuer=peer.issuer, scope=peer.scope)))
    auth = await servers.oauth_for(servers.configs()[0])
    assert auth is not None
    try:
        # A user may leave the browser open indefinitely; the backend must release the flow.
        monkeypatch.setattr("ava.tool.mcp_oauth.LOGIN_SECONDS", 0.5)
        start = await auth.start()
        await asyncio.wait_for(auth.task, timeout=2)
        assert auth.status == "required" and "timed out" in auth.error
        with pytest.raises(ValueError, match="active OAuth"):
            await auth.complete(start["flow_id"], code="late-code", state=parse_qs(urlsplit(start["authorization_url"]).query)["state"][0])
        assert auth.data["tokens"] is None
        assert peer.client_secret not in caplog.text
        monkeypatch.setattr("ava.tool.mcp_oauth.LOGIN_SECONDS", 10)
        original = httpx2.AsyncClient
        async with original() as network:
            async def reject_token(request):
                if request.url.path == "/token":
                    return httpx2.Response(400, json={"error": "invalid_grant", "error_description": peer.client_secret + " leaked-access-token"})
                return await network.send(request)
            monkeypatch.setattr("ava.tool.mcp_oauth.httpx2.AsyncClient", lambda **kwargs: original(transport=httpx2.MockTransport(reject_token), **kwargs))
            start = await auth.start()
            with pytest.raises(ValueError, match="OAuth sign-in failed"):
                await auth.complete(start["flow_id"], code="bad-code", state=parse_qs(urlsplit(start["authorization_url"]).query)["state"][0], iss=peer.issuer)
        assert peer.client_secret not in caplog.text and "leaked-access-token" not in caplog.text
        assert auth.data["tokens"] is None
    finally:
        await servers.aclose()
        peer.close()
