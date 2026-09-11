"""A loopback OAuth authority and Gmail-shaped MCP peer; no external credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest


class OAuthServer:
    client_id = "ava-test-client"
    client_secret = "test-client-secret-never-expose"
    scope = "mail.readonly"

    def __init__(self):
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, float] = {}
        self.refresh_tokens: set[str] = set()
        self.authorizations = self.refreshes = self.calls = 0
        self.ttl = 60
        self.public_catalog = True
        self.reject_refresh = False
        self.insufficient_scope = False
        self.refresh_started = threading.Event()
        self.refresh_gate: threading.Event | None = None
        self.last_authorization: dict = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, value=None, headers=None):
                body = json.dumps(value).encode() if value is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path)
                if path.path.startswith("/.well-known/oauth-protected-resource"):
                    self.respond(200, {"resource": owner.url, "authorization_servers": [owner.issuer],
                                       "scopes_supported": [owner.scope, "mail.full_access"]})
                elif path.path == "/.well-known/oauth-authorization-server":
                    self.respond(200, {"issuer": owner.issuer, "authorization_endpoint": owner.issuer + "/authorize",
                                       "token_endpoint": owner.issuer + "/token", "registration_endpoint": owner.issuer + "/register",
                                       "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                                       "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
                                       "authorization_response_iss_parameter_supported": True})
                elif path.path == "/authorize":
                    params = {key: values[0] for key, values in parse_qs(path.query).items()}
                    assert params["client_id"] == owner.client_id
                    assert params["code_challenge_method"] == "S256"
                    owner.last_authorization = params
                    owner.authorizations += 1
                    code = secrets.token_urlsafe(24)
                    owner.codes[code] = params
                    self.respond(302, headers={"Location": params["redirect_uri"] + "?" + urlencode({
                        "code": code, "state": params["state"], "iss": owner.issuer})})
                elif path.path == "/mcp":
                    self.respond(405)
                else:
                    self.respond(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/register":
                    value = json.loads(body)
                    self.respond(201, {**value, "client_id": owner.client_id, "token_endpoint_auth_method": "none"})
                    return
                if self.path == "/token":
                    params = {key: values[0] for key, values in parse_qs(body.decode()).items()}
                    if params.get("client_id") != owner.client_id or params.get("client_secret", owner.client_secret) != owner.client_secret:
                        self.respond(401, {"error": "invalid_client"})
                        return
                    if params.get("grant_type") == "refresh_token":
                        if owner.reject_refresh or params.get("refresh_token") not in owner.refresh_tokens:
                            self.respond(400, {"error": "invalid_grant", "error_description": owner.client_secret})
                            return
                        owner.refresh_tokens.remove(params["refresh_token"])
                        owner.refreshes += 1
                        owner.refresh_started.set()
                        if owner.refresh_gate is not None:
                            owner.refresh_gate.wait(10)
                    else:
                        code = owner.codes.pop(params.get("code", ""), None)
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(params.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                        if not code or code["code_challenge"] != challenge or code["redirect_uri"] != params.get("redirect_uri"):
                            self.respond(400, {"error": "invalid_grant"})
                            return
                    token, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
                    owner.tokens[token] = time.time() + owner.ttl
                    owner.refresh_tokens.add(refresh)
                    self.respond(200, {"access_token": token, "refresh_token": refresh, "token_type": "Bearer",
                                       "expires_in": owner.ttl, "scope": owner.scope})
                    return
                if self.path != "/mcp":
                    self.respond(404)
                    return
                message = json.loads(body)
                method = message["method"]
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if (method == "tools/call" or not owner.public_catalog) and owner.tokens.get(token, 0) <= time.time():
                    self.respond(401, {"error": "unauthorized"}, {"WWW-Authenticate":
                        f'Bearer resource_metadata="{owner.issuer}/.well-known/oauth-protected-resource/mcp"'})
                    return
                if method == "tools/call" and owner.insufficient_scope:
                    self.respond(403, {"error": "insufficient_scope"}, {"WWW-Authenticate":
                        f'Bearer error="insufficient_scope", scope="mail.full_access", resource_metadata="{owner.issuer}/.well-known/oauth-protected-resource/mcp"'})
                    return
                if "id" not in message:
                    self.respond(202)
                    return
                if method == "server/discover":
                    self.respond(200, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Legacy peer"}})
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "OAuth mail fixture", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "list_labels", "description": "List mailbox labels without modifying mail.",
                                         "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}]}
                elif method == "tools/call":
                    owner.calls += 1
                    result = {"content": [{"type": "text", "text": "INBOX, Important"}]}
                else:
                    result = {}
                self.respond(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.url = self.issuer + "/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.refresh_gate is not None:
            self.refresh_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@pytest.fixture
def oauth_server():
    server = OAuthServer()
    try:
        yield server
    finally:
        server.close()
