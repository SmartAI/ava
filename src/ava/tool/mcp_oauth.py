"""MCP SDK OAuth with backend-owned credentials and an explicit desktop login handoff.

The SDK owns discovery, PKCE, state/issuer checks, registration, exchange and refresh.
Only user-initiated sign-in may wait for a browser; agent requests never open one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx2
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, OAuthFlowError
from mcp.client.auth.utils import extract_field_from_www_auth
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

REDIRECT_URI = "http://127.0.0.1:8766/oauth/callback"
GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
LOGIN_SECONDS = 300


def _private_sdk_log(record: logging.LogRecord) -> bool:
    # The SDK logs exception bodies, including untrusted token responses and callback state.
    if record.exc_info:
        record.msg, record.args = "MCP OAuth failed; see the server's authentication status.", ()
        record.exc_info = record.exc_text = None
    return True


logging.getLogger("mcp.client.auth.oauth2").addFilter(_private_sdk_log)


def safe_url(value: str) -> None:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("OAuth URLs must use HTTPS (HTTP is allowed only on loopback).")


class OAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: str = Field(default="", max_length=2048)
    client_secret: str = Field(default="", max_length=4096, repr=False)
    issuer: str = Field(default="", max_length=2048)
    scope: str = Field(default="", max_length=4096)
    redirect_uri: str = Field(default=REDIRECT_URI, max_length=2048)

    @model_validator(mode="after")
    def valid(self):
        self.client_id, self.issuer, self.scope = self.client_id.strip(), self.issuer.strip(), " ".join(self.scope.split())
        if self.client_secret and not self.client_id:
            raise ValueError("An OAuth client secret requires a client ID.")
        if self.client_id and not self.issuer:
            raise ValueError("Provide the authorization server (issuer) for this OAuth client ID.")
        if self.issuer:
            safe_url(self.issuer)
            if urlsplit(self.issuer).query:
                raise ValueError("The OAuth issuer must not contain a query.")
        parsed = urlsplit(self.redirect_uri)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.path != "/oauth/callback" or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("Use a loopback callback such as http://127.0.0.1:8766/oauth/callback.")
        return self


def oauth_binding(config: dict) -> str:
    return hashlib.sha256(json.dumps({"url": config.get("url"), "oauth": config.get("oauth")}, sort_keys=True).encode()).hexdigest()


class AuthenticationRequired(OAuthFlowError):
    def __init__(self):
        super().__init__("Sign in to this server in MCP settings, then retry the tool call.")


def auth_error(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "OAuth sign-in timed out. Try signing in again."
    if isinstance(error, AuthenticationRequired):
        return str(error)
    message = str(error)
    if "issuer" in message.lower() or "response iss" in message.lower() or "resource" in message.lower() and "match" in message.lower():
        return "OAuth server identity validation failed. Check the issuer and MCP URL."
    if "state" in message.lower():
        return "The OAuth callback did not match this sign-in attempt. Try again."
    if "registration" in message.lower() or "client info" in message.lower():
        return "OAuth client registration failed. This server may require a client ID and secret in settings."
    return "OAuth sign-in failed. Check the client settings, callback URL and permissions, then retry."


class MCPOAuth(OAuthClientProvider):
    """One SDK auth instance per server, shared by its workspace connections."""

    def __init__(self, home: Path, config: dict, changed: Callable[[], None]):
        self.home, self.config, self.changed = home, config, changed
        self.settings = OAuthConfig.model_validate(config["oauth"])
        self.binding = oauth_binding(config)
        self.data: dict = {}
        self.status, self.error, self.challenge = "required", "", ""
        self.task: asyncio.Task | None = None
        self.flow_id = self.authorization_url = self.expected_state = ""
        self.callback: asyncio.Future[AuthorizationCodeResult] | None = None
        self.ready = asyncio.Event()
        self.retired = False
        self.loading: asyncio.Task | None = None
        metadata = OAuthClientMetadata(client_name="Ava", redirect_uris=[AnyUrl(self.settings.redirect_uri)],
                                       scope=self.settings.scope or None,
                                       token_endpoint_auth_method="client_secret_post" if self.settings.client_secret else "none")
        super().__init__(server_url=config["url"], client_metadata=metadata, storage=self,
                         redirect_handler=self._redirect, callback_handler=self._callback)

    def _database(self, write: str | None = None, epoch: str = "") -> dict:
        path = self.home / "capabilities.sqlite3"
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            if write is not None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config FROM mcp_servers WHERE id=?", (self.config["id"],)).fetchone()
            if not row or oauth_binding(json.loads(row[0])) != self.binding:
                raise AuthenticationRequired()
            db.execute("CREATE TABLE IF NOT EXISTS mcp_oauth (id TEXT PRIMARY KEY, binding TEXT NOT NULL, data TEXT NOT NULL)")
            row = db.execute("SELECT data FROM mcp_oauth WHERE id=? AND binding=?", (self.config["id"], self.binding)).fetchone()
            stored = json.loads(row[0]) if row else {}
            if write is not None:
                # Signing out/cancelling invalidates even an already-running refresh write.
                if stored.get("epoch", "") != epoch:
                    raise AuthenticationRequired()
                db.execute("INSERT OR REPLACE INTO mcp_oauth VALUES (?,?,?)", (self.config["id"], self.binding, write))
            return stored

    async def load(self) -> None:
        self.data = await asyncio.to_thread(self._database)
        await self._initialize()
        self.status = "authorized" if self.context.current_tokens else "required"

    async def _initialize(self) -> None:
        await super()._initialize()
        # The SDK's TokenStorage only stores relative expires_in. Restore the absolute
        # expiry and validated AS metadata too, so restart refreshes at the correct endpoint.
        self.context.token_expiry_time = self.data.get("expires_at")
        if self.data.get("metadata"):
            self.context.oauth_metadata = OAuthMetadata.model_validate(self.data["metadata"])
        self.context.auth_server_url = self.data.get("issuer")
        if self.data.get("resource_metadata"):
            self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(self.data["resource_metadata"])
        self._configure_client_auth()

    def _configure_client_auth(self) -> None:
        if self.settings.client_secret and self.context.client_info:
            metadata = self.context.oauth_metadata
            methods = metadata.token_endpoint_auth_methods_supported if metadata else None
            if methods and "client_secret_post" not in methods and "client_secret_basic" in methods:
                self.context.client_info.token_endpoint_auth_method = "client_secret_basic"

    async def get_tokens(self) -> OAuthToken | None:
        return OAuthToken.model_validate(self.data["tokens"]) if self.data.get("tokens") else None

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if self.settings.client_id:
            return OAuthClientInformationFull(client_id=self.settings.client_id, client_secret=self.settings.client_secret or None,
                                              issuer=self.settings.issuer, redirect_uris=[AnyUrl(self.settings.redirect_uri)],
                                              token_endpoint_auth_method="client_secret_post" if self.settings.client_secret else "none")
        return OAuthClientInformationFull.model_validate(self.data["client"]) if self.data.get("client") else None

    async def _persist(self) -> None:
        if self.retired:
            raise AuthenticationRequired()
        metadata = self.context.oauth_metadata
        resource = self.context.protected_resource_metadata
        self.data.update(metadata=metadata.model_dump(mode="json") if metadata else None,
                         resource_metadata=resource.model_dump(mode="json") if resource else None,
                         issuer=self.context.auth_server_url)
        await asyncio.to_thread(self._database, json.dumps(self.data), self.data.get("epoch", ""))

    async def _forget_tokens(self) -> None:
        cleared = {**self.data, "tokens": None, "expires_at": None, "epoch": secrets.token_hex(16)}
        await asyncio.to_thread(self._database, json.dumps(cleared), self.data.get("epoch", ""))
        self.data = cleared
        self.context.clear_tokens()

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.data.update(tokens=tokens.model_dump(), expires_at=self.context.token_expiry_time)
        await self._persist()
        self.status, self.error = "authorized", ""
        self.changed()

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.data["client"] = client_info.model_dump(mode="json")
        await self._persist()

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        ok = await super()._handle_refresh_response(response)
        if not ok:
            self.data.update(tokens=None, expires_at=None)
            await self._persist()
            self.status, self.error = "required", "Your authorization expired or was revoked. Sign in again."
            self.changed()
        return ok

    def _select_authorization_server(self, servers: list[str]) -> str:
        # Gmail's PRM spells Google's issuer with '/', while Google's RFC 8414
        # metadata uses the canonical spelling below. Do not relax issuer checks
        # for any other server or any callback (RFC 9207).
        if urlsplit(self.config["url"]).hostname == "gmailmcp.googleapis.com":
            servers = [GOOGLE_ISSUER if issuer == GOOGLE_ISSUER + "/" else issuer for issuer in servers]
        if self.settings.issuer and self.settings.issuer not in servers:
            raise OAuthFlowError("Configured OAuth issuer does not match the server.")
        selected = self.settings.issuer or super()._select_authorization_server(servers)
        safe_url(selected)
        return selected

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        self._configure_client_auth()
        if self.settings.scope:
            # SDK discovery otherwise replaces configured scopes with every advertised
            # scope. Explicit user permissions are a ceiling, not an escalation hint.
            self.context.client_metadata.scope = self.settings.scope
        return await super()._perform_authorization_code_grant()

    async def _redirect(self, url: str) -> None:
        if asyncio.current_task() is not self.task:
            raise AuthenticationRequired()
        safe_url(url)
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        if self.context.oauth_metadata and str(self.context.oauth_metadata.issuer) == GOOGLE_ISSUER:
            params.update(access_type=["offline"], prompt=["consent"])
            url = urlunsplit(parsed._replace(query=urlencode(params, doseq=True)))
        self.expected_state = params["state"][0]
        self.authorization_url = url
        self.ready.set()
        self.changed()

    async def _callback(self) -> AuthorizationCodeResult:
        if self.callback is None:
            raise AuthenticationRequired()
        return await self.callback

    async def _auth_flow(self, request: httpx2.Request):
        interactive = asyncio.current_task() is self.task
        if self.retired or not interactive and self.task and not self.task.done():
            raise AuthenticationRequired()
        attempted_refresh = not self.context.is_token_valid() and self.context.can_refresh_token()
        flow = super()._auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                safe_url(str(outgoing.url))
                # Never let a public MCP server direct discovery to a local HTTP service.
                if urlsplit(self.config["url"]).scheme == "https" and outgoing.url.scheme == "http":
                    raise OAuthFlowError("OAuth discovery must not downgrade to HTTP.")
                response = yield outgoing
                if outgoing is request and response.status_code == 401 and not interactive and not attempted_refresh and self.context.can_refresh_token():
                    # An access token can be invalidated before its advertised expiry.
                    # Reuse the SDK's refresh/exchange machinery once before asking to sign in.
                    attempted_refresh = True
                    refresh_request = await self._refresh_token()
                    safe_url(str(refresh_request.url))
                    refreshed = yield refresh_request
                    if await self._handle_refresh_response(refreshed):
                        self._add_auth_header(request)
                        response = yield request
                step_up = response.status_code == 403 and extract_field_from_www_auth(response, "error") == "insufficient_scope"
                if outgoing is request and (response.status_code == 401 or step_up):
                    self.challenge = response.headers.get("www-authenticate", "")[:8192]
                    if not interactive:
                        self.status = "required"
                        self.error = "This tool needs additional permissions. Edit this server's OAuth scopes and sign in again." if step_up else str(AuthenticationRequired())
                        self.changed()
                        raise AuthenticationRequired()
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()

    async def start(self) -> dict:
        if self.retired:
            raise ValueError("This server changed. Refresh before signing in.")
        if self.task and not self.task.done():
            raise ValueError("Sign-in is already in progress. Complete it in the original desktop or wait for it to expire.")
        if self.task is None or self.task.done():
            self.flow_id = secrets.token_urlsafe(32)
            self.authorization_url = self.expected_state = ""
            self.ready = asyncio.Event()
            self.callback = asyncio.get_running_loop().create_future()
            self.status, self.error = "signing_in", ""
            self.task = asyncio.create_task(self._login(), name=f"mcp-oauth-{self.config['id']}")
            self.changed()
        try:
            async with asyncio.timeout(20):
                await self.ready.wait()
        except TimeoutError:
            await self.cancel()
            raise ValueError("OAuth discovery timed out. Check this server and retry.") from None
        if not self.authorization_url:
            raise ValueError(self.error or "Could not start OAuth sign-in.")
        return {"flow_id": self.flow_id, "authorization_url": self.authorization_url}

    async def _login(self) -> None:
        # Explicit sign-in bootstraps the SDK with the last challenge (or well-known
        # discovery), even for servers like Gmail with a public tools/list. Drive the
        # public httpx auth generator, but NEVER execute/replay a tool to trigger login.
        request = httpx2.Request("POST", self.config["url"], headers={"MCP-Protocol-Version": "2026-07-28"})
        flow = self.async_auth_flow(request)
        try:
            async with asyncio.timeout(LOGIN_SECONDS), httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
                async with self.context.lock:
                    self.context.clear_tokens()
                    self.data.update(tokens=None, expires_at=None)
                    await self._persist()
                outgoing = await anext(flow)
                if outgoing is not request:
                    raise OAuthFlowError("Unexpected initial OAuth request from the SDK.")
                response = httpx2.Response(401, headers={"WWW-Authenticate": self.challenge}, request=request)
                while True:
                    outgoing = await flow.asend(response)
                    if outgoing is request:
                        break  # The SDK exchanged and stored the token. No tool side effects.
                    streamed = await client.send(outgoing, stream=True)
                    content = bytearray()
                    try:
                        async for chunk in streamed.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 1024 * 1024:
                                raise OAuthFlowError("OAuth metadata or token response is too large.")
                    finally:
                        await streamed.aclose()
                    headers = {key: value for key, value in streamed.headers.items()
                               if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                    response = httpx2.Response(streamed.status_code, headers=headers,
                                               content=bytes(content), request=outgoing)
                    response.next_request = streamed.next_request
        except asyncio.CancelledError:
            self.status, self.error = "required", "Sign-in cancelled."
        except Exception as error:
            self.status, self.error = "required", auth_error(error)
        finally:
            await flow.aclose()
            if self.status != "authorized" and not self.retired:
                try:
                    await self._forget_tokens()
                except AuthenticationRequired:
                    pass  # A config edit/removal already invalidated this login's writes.
            if self.callback is not None and not self.callback.done():
                self.callback.cancel()
            self.authorization_url = self.expected_state = ""
            self.ready.set()
            self.changed()

    async def complete(self, flow_id: str, *, code: str = "", state: str = "", iss: str | None = None, error: str = "") -> None:
        if (not self.task or self.task.done() or not self.callback or self.callback.done()
                or not secrets.compare_digest(flow_id.encode(), self.flow_id.encode()) or not self.expected_state
                or not secrets.compare_digest(state.encode(), self.expected_state.encode())):
            raise ValueError("This callback does not match an active OAuth sign-in.")
        if error:
            await self.cancel()
            self.error = "Sign-in was denied. You can try again when ready."
            self.changed()
            return
        if not code:
            raise ValueError("The OAuth callback did not include an authorization code.")
        self.callback.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
        await self.task
        if self.status != "authorized":
            raise ValueError(self.error)

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task

    async def sign_out(self) -> None:
        self.retired = True
        await self.cancel()
        await self._forget_tokens()
        self.status, self.error = "required", ""
        self.changed()

    async def close(self) -> None:
        self.retired = True
        await self.cancel()
