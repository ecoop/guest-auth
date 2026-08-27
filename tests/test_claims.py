# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Tests for claims resolution (role / scopes) on the guest identity.

Covers:
    - No resolver → identity carries no claims (pre-claims behaviour)
    - Sync and async resolvers, plus an async callable *object*
    - A sync resolver runs off the event loop thread (never blocks it)
    - Resolver returning None → empty claims
    - Resolver raising → request still succeeds, claims are None, logged
    - The resolver runs only on authenticated requests
    - ``GuestIdentity`` stays hashable once claims are attached
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guest_auth import (
    COOKIE_NAME,
    GuestClaims,
    GuestIdentity,
    InviteAuthMiddleware,
    get_current_guest,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


@dataclass
class FakeConfig:
    demo_mode: bool = True
    invite_tokens: dict = field(default_factory=dict)


def _make_app(config: FakeConfig, resolver=None) -> FastAPI:
    """App exposing the current identity's claims, plus the loop thread id."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami():
        guest = get_current_guest()
        return {
            "recipient": guest.recipient if guest else None,
            "role": guest.role if guest else None,
            "scopes": guest.scopes if guest else None,
            "loop_thread": threading.get_ident(),
        }

    app.add_middleware(InviteAuthMiddleware, config=config, claims_resolver=resolver)
    return app


@pytest.fixture
def config() -> FakeConfig:
    return FakeConfig(demo_mode=True, invite_tokens={"tok_good": "Jane Tester"})


def _authed(config: FakeConfig, resolver=None) -> TestClient:
    client = TestClient(_make_app(config, resolver))
    client.cookies.set(COOKIE_NAME, "tok_good")
    return client


# ── Resolver shapes ──────────────────────────────────────────────────────────


def test_no_resolver_means_no_claims(config):
    """Omitting the resolver reproduces the pre-claims identity exactly."""
    r = _authed(config).get("/whoami")
    assert r.status_code == 200
    assert r.json()["recipient"] == "Jane Tester"
    assert r.json()["role"] is None
    assert r.json()["scopes"] is None


def test_sync_resolver_attaches_claims(config):
    def resolver(token: str) -> GuestClaims:
        assert token == "tok_good"
        return GuestClaims(role="level5", scopes=("rules", "faq"))

    body = _authed(config, resolver).get("/whoami").json()
    assert body["role"] == "level5"
    assert body["scopes"] == ["rules", "faq"]  # tuple → JSON array


def test_async_resolver_attaches_claims(config):
    async def resolver(token: str) -> GuestClaims:
        return GuestClaims(role="level8", scopes=())

    body = _authed(config, resolver).get("/whoami").json()
    assert body["role"] == "level8"
    assert body["scopes"] == []


def test_async_callable_object_is_awaited_not_threadpooled(config):
    """Async-ness lives on ``__call__`` for a callable object; detect it there.

    If this were misdetected as sync, ``run_in_threadpool`` would hand back
    an un-awaited coroutine and ``role`` would be a coroutine object, not a
    string (and the test would leak a "never awaited" warning).
    """

    class Resolver:
        async def __call__(self, token: str) -> GuestClaims:
            return GuestClaims(role="level6", scopes=("decks",))

    body = _authed(config, Resolver()).get("/whoami").json()
    assert body["role"] == "level6"


def test_sync_resolver_runs_off_the_event_loop_thread(config):
    """A blocking resolver must not run on the loop thread.

    Rulebook's real resolver reads a GCS object on a cache miss. Called
    inline in the ASGI middleware, that would stall every other request
    on the worker for the duration of a network round trip.
    """
    seen: dict[str, int] = {}

    def resolver(token: str) -> GuestClaims:
        seen["resolver_thread"] = threading.get_ident()
        return GuestClaims(role="level1")

    body = _authed(config, resolver).get("/whoami").json()
    assert seen["resolver_thread"] != body["loop_thread"]


# ── Absence and failure ──────────────────────────────────────────────────────


def test_resolver_returning_none_means_empty_claims(config):
    body = _authed(config, lambda token: None).get("/whoami").json()
    assert body["recipient"] == "Jane Tester"
    assert body["role"] is None
    assert body["scopes"] is None


def test_resolver_failure_is_soft(config, caplog):
    """A broken claim store degrades to 'no claims', never to a 401.

    The token is still valid — we just don't know what it may do. Note the
    contract this puts on the host: ``role=None`` here is *unresolved*, so
    a policy layer that reads None as permissive would fail open during
    exactly this outage.
    """

    def resolver(token: str) -> GuestClaims:
        raise RuntimeError("bucket unreachable")

    with caplog.at_level(logging.ERROR):
        r = _authed(config, resolver).get("/whoami")

    assert r.status_code == 200
    assert r.json()["recipient"] == "Jane Tester"
    assert r.json()["role"] is None
    assert "claims resolver failed" in caplog.text


def test_async_resolver_failure_is_soft(config):
    async def resolver(token: str) -> GuestClaims:
        raise RuntimeError("db down")

    r = _authed(config, resolver).get("/whoami")
    assert r.status_code == 200
    assert r.json()["role"] is None


# ── When the resolver runs ───────────────────────────────────────────────────


def test_resolver_not_called_without_a_valid_cookie(config):
    calls: list[str] = []

    def resolver(token: str) -> GuestClaims:
        calls.append(token)
        return GuestClaims(role="level5")

    client = TestClient(_make_app(config, resolver))

    assert client.get("/whoami").status_code == 401           # no cookie
    client.cookies.set(COOKIE_NAME, "tok_bogus")
    assert client.get("/whoami").status_code == 401           # bad cookie
    assert calls == []


def test_resolver_not_called_when_demo_mode_off(config):
    calls: list[str] = []

    def resolver(token: str) -> GuestClaims:
        calls.append(token)
        return GuestClaims(role="level5")

    config.demo_mode = False
    client = _authed(config, resolver)
    assert client.get("/whoami").status_code == 200
    assert calls == []


# ── Identity invariants ──────────────────────────────────────────────────────


def test_identity_stays_hashable_with_claims():
    """Regression guard: ``scopes`` must never become a mutable container.

    A ``list`` field would leave ``GuestIdentity`` hashable while claims
    are absent and blow up at the first request that carries them — so
    the failure would land in production, not here.
    """
    identity = GuestIdentity("tok", "Jane", role="level5", scopes=("a", "b"))
    assert hash(identity)
    assert {identity: 1}[identity] == 1
    assert len({identity, GuestIdentity("tok", "Jane", "level5", ("a", "b"))}) == 1


def test_identity_positional_construction_unchanged():
    """The pre-claims two-arg form still works — this is an additive change."""
    identity = GuestIdentity("tok", "Jane")
    assert (identity.token, identity.recipient) == ("tok", "Jane")
    assert identity.role is None and identity.scopes is None
