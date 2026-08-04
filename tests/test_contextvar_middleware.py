# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Library-level tests for ``PathScopedContextVarMiddleware``.

Exercises the generic middleware directly:

    - value stored on match, ``None`` on non-match, reset after each request
    - multiple middleware instances publish independent ContextVars without
      interference
    - the value survives into a *sync* endpoint that runs in Starlette's
      threadpool (the whole reason this is a pure-ASGI middleware rather
      than ``BaseHTTPMiddleware``)
    - a custom extractor can pick a different capture group / transform
"""

from __future__ import annotations

import re
from contextvars import ContextVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from guest_auth.contextvar_middleware import PathScopedContextVarMiddleware

# Fresh ContextVars per test module to avoid cross-test bleed; each fixture
# builds its own app so nothing shares middleware instances either.

def _fresh_var(name: str) -> ContextVar[str | None]:
    return ContextVar(name, default=None)


def test_match_publishes_extracted_value_to_contextvar():
    var = _fresh_var("test_alpha")
    app = FastAPI()

    @app.get("/things/{name}/detail")
    def detail(name: str):
        return {"seen": var.get()}

    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/things/([^/]+)/"),
        contextvar=var,
    )
    client = TestClient(app)

    r = client.get("/things/widget/detail")
    assert r.json() == {"seen": "widget"}


def test_non_matching_path_leaves_contextvar_unset():
    var = _fresh_var("test_beta")
    app = FastAPI()

    @app.get("/other")
    def other():
        return {"seen": var.get()}

    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/things/([^/]+)/"),
        contextvar=var,
    )
    client = TestClient(app)

    r = client.get("/other")
    assert r.json() == {"seen": None}


def test_contextvar_resets_between_requests():
    var = _fresh_var("test_gamma")
    app = FastAPI()

    @app.get("/things/{name}/detail")
    def detail(name: str):
        return {"seen": var.get()}

    @app.get("/other")
    def other():
        return {"seen": var.get()}

    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/things/([^/]+)/"),
        contextvar=var,
    )
    client = TestClient(app)

    assert client.get("/things/first/detail").json() == {"seen": "first"}
    # After the first request the middleware must reset — the second
    # request goes to a non-matching path, so the value must be None
    # (not the stale "first").
    assert client.get("/other").json() == {"seen": None}
    # And outside any request the module-level ContextVar is back to default.
    assert var.get() is None


def test_multiple_middlewares_publish_independent_contextvars():
    left = _fresh_var("test_delta_left")
    right = _fresh_var("test_delta_right")
    app = FastAPI()

    @app.get("/left/{value}/x")
    def left_endpoint(value: str):
        return {"left": left.get(), "right": right.get()}

    @app.get("/right/{value}/x")
    def right_endpoint(value: str):
        return {"left": left.get(), "right": right.get()}

    # Two independent middleware instances, each with its own pattern +
    # ContextVar. Registered in one order; both must apply.
    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/right/([^/]+)/"),
        contextvar=right,
    )
    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/left/([^/]+)/"),
        contextvar=left,
    )
    client = TestClient(app)

    # Only the matching middleware fires per request; the other var stays None.
    assert client.get("/left/L/x").json() == {"left": "L", "right": None}
    assert client.get("/right/R/x").json() == {"left": None, "right": "R"}


def test_sync_endpoint_sees_value_from_threadpool():
    """Load-bearing: pure-ASGI middleware propagates into the threadpool.

    ``def`` (not ``async def``) endpoints run in Starlette's threadpool,
    and downstream sync calls run there too. This is the exact reason
    the middleware is pure-ASGI rather than ``BaseHTTPMiddleware`` —
    regressing this would silently break per-request identity
    propagation into sync code paths.
    """
    var = _fresh_var("test_epsilon")
    app = FastAPI()

    @app.get("/things/{name}/detail")
    def sync_detail(name: str):
        # Sync endpoint: FastAPI runs this in the threadpool. Reading the
        # ContextVar here proves the middleware's context copy reached
        # the pool worker.
        return {"seen": var.get()}

    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/things/([^/]+)/"),
        contextvar=var,
    )
    client = TestClient(app)

    r = client.get("/things/from-pool/detail")
    assert r.json() == {"seen": "from-pool"}


def test_string_pattern_is_compiled():
    var = _fresh_var("test_zeta")
    app = FastAPI()

    @app.get("/things/{name}/detail")
    def detail(name: str):
        return {"seen": var.get()}

    # Pass a raw string; the middleware should compile it internally.
    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=r"^/things/([^/]+)/",
        contextvar=var,
    )
    client = TestClient(app)

    assert client.get("/things/str/detail").json() == {"seen": "str"}


def test_custom_extractor_can_pick_named_group():
    var = _fresh_var("test_eta")
    app = FastAPI()

    @app.get("/{owner}/{repo}/blob")
    def blob(owner: str, repo: str):
        return {"seen": var.get()}

    app.add_middleware(
        PathScopedContextVarMiddleware,
        pattern=re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/"),
        contextvar=var,
        extractor=lambda m: f"{m.group('owner')}:{m.group('repo')}",
    )
    client = TestClient(app)

    r = client.get("/acme/widgets/blob")
    assert r.json() == {"seen": "acme:widgets"}


def test_non_http_scope_is_passed_through_unchanged():
    """Lifespan and websocket scopes must not touch the ContextVar."""
    var = _fresh_var("test_theta")
    captured: list[dict] = []

    async def downstream(scope, receive, send):
        # Record what downstream sees; the middleware should NOT have
        # set the ContextVar for a non-http scope.
        captured.append({"type": scope["type"], "value": var.get()})

    mw = PathScopedContextVarMiddleware(
        downstream,
        pattern=re.compile(r"^/x/([^/]+)/"),
        contextvar=var,
    )

    import asyncio

    async def _noop_receive():
        return {"type": "lifespan.startup"}

    async def _noop_send(message):
        pass

    asyncio.run(mw({"type": "lifespan"}, _noop_receive, _noop_send))

    assert captured == [{"type": "lifespan", "value": None}]
