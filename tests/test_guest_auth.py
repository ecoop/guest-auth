# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Tests for the invite-token auth gate.

Covers:
    - Middleware no-op when demo_mode is off
    - Token-in-URL exchange: 302 + Set-Cookie (httpOnly/Secure/SameSite=Lax),
      clean Location
    - Invalid URL token → 401 friendly page
    - Valid cookie → request passes through
    - Missing / invalid cookie → 401
    - Identity ContextVar set during the request and reset afterward
    - Custom ``welcome_html`` served verbatim; default fallback used when omitted
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guest_auth import (
    COOKIE_NAME,
    GuestIdentity,
    InviteAuthMiddleware,
    get_current_guest,
)

# ── Fake config + app helpers ────────────────────────────────────────────────


@dataclass
class FakeConfig:
    """Minimal ``GuestAuthConfig`` for tests. Mutable so ``monkeypatch``-style
    flips of ``demo_mode`` / ``invite_tokens`` on a live instance still work.
    """

    demo_mode: bool = True
    invite_tokens: dict = field(default_factory=dict)


def _make_app(config: FakeConfig, *, welcome_html: str | None = None) -> FastAPI:
    """Bare FastAPI app with a couple endpoints wrapped in the gate."""
    app = FastAPI()

    @app.get("/")
    def root():
        return {"ok": True}

    @app.get("/api/config")
    def api_config():
        return {"who": get_current_guest().recipient if get_current_guest() else None}

    app.add_middleware(InviteAuthMiddleware, config=config, welcome_html=welcome_html)
    return app


# ── HTTP behaviour via TestClient ────────────────────────────────────────────


@pytest.fixture
def config() -> FakeConfig:
    return FakeConfig(demo_mode=True, invite_tokens={"tok_good": "Jane Tester"})


@pytest.fixture
def client(config: FakeConfig) -> TestClient:
    return TestClient(_make_app(config))


def test_demo_off_is_passthrough(config):
    """With demo_mode off, no cookie is needed — the gate is invisible."""
    config.demo_mode = False
    client = TestClient(_make_app(config))
    r = client.get("/api/config")
    assert r.status_code == 200


def test_no_credential_returns_401(client):
    r = client.get("/api/config")
    assert r.status_code == 401
    # Default fallback body advertises "invite-only" — the built-in
    # welcome copy that ships when the host doesn't supply its own.
    assert "invite-only" in r.text.lower()


def test_valid_token_in_url_sets_cookie_and_redirects(client):
    r = client.get("/?token=tok_good", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    set_cookie = r.headers["set-cookie"]
    assert f"{COOKIE_NAME}=tok_good" in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=2592000" in lowered  # 30 days


def test_clean_location_preserves_other_query_params(client):
    r = client.get("/?token=tok_good&tab=pipeline", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/?tab=pipeline"


def test_invalid_token_in_url_returns_401(client):
    r = client.get("/?token=tok_bogus", follow_redirects=False)
    assert r.status_code == 401


def test_valid_cookie_passes_through(client):
    client.cookies.set(COOKIE_NAME, "tok_good")
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json() == {"who": "Jane Tester"}


def test_invalid_cookie_returns_401(client):
    client.cookies.set(COOKIE_NAME, "tok_bogus")
    r = client.get("/api/config")
    assert r.status_code == 401


def test_custom_welcome_html_is_served_verbatim(config):
    """Host-supplied ``welcome_html`` is injected into the library's
    page chrome unchanged — the library takes pre-rendered HTML and
    stays out of the content authoring business.
    """
    body = "<h1>Custom Host Welcome</h1><p>Sentinel body.</p>"
    client = TestClient(_make_app(config, welcome_html=body))
    r = client.get("/api/config")
    assert r.status_code == 401
    assert "Custom Host Welcome" in r.text
    assert "Sentinel body." in r.text


def test_default_welcome_html_falls_back_to_builtin(config):
    """Omitting ``welcome_html`` gives a generic library-provided body,
    so a host that forgot to configure a welcome message still serves
    a functional 401 page.
    """
    client = TestClient(_make_app(config))
    r = client.get("/api/config")
    assert r.status_code == 401
    assert "invite-only" in r.text.lower()


# ── Identity propagation (direct ASGI drive) ─────────────────────────────────


def _make_scope(*, path="/api/config", query=b"", cookie=None, method="GET"):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers,
    }


def _drive(app, scope) -> list[dict]:
    """Run an ASGI app once against a scope; return the messages it sent."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def test_identity_set_during_request_and_reset_after(config):
    captured: dict[str, GuestIdentity | None] = {}

    async def fake_app(scope, receive, send):
        captured["identity"] = get_current_guest()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = InviteAuthMiddleware(fake_app, config=config)
    _drive(mw, _make_scope(cookie="tok_good"))

    ident = captured["identity"]
    assert ident is not None
    assert ident.token == "tok_good"
    assert ident.recipient == "Jane Tester"
    # ContextVar is reset once the request completes.
    assert get_current_guest() is None


def test_invalid_cookie_does_not_reach_app(config):
    reached = {"called": False}

    async def fake_app(scope, receive, send):
        reached["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = InviteAuthMiddleware(fake_app, config=config)
    sent = _drive(mw, _make_scope(cookie="tok_bogus"))

    assert reached["called"] is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


def test_mutating_config_at_runtime_takes_effect(config):
    """``monkeypatch.setattr``-style flips on a live config instance
    are picked up on the next request without rebuilding the middleware.
    This is why the middleware reads ``demo_mode`` / ``invite_tokens``
    at request time via attribute lookup, not at construction.
    """
    client = TestClient(_make_app(config))

    # First request: bad cookie → 401.
    client.cookies.set(COOKIE_NAME, "tok_new")
    assert client.get("/api/config").status_code == 401

    # Rotate the allowlist on the same config instance.
    config.invite_tokens = {"tok_new": "New Tester"}
    assert client.get("/api/config").status_code == 200
