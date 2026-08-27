# Integration Guide

How to adopt `guest-auth` in a Python ASGI application. This is the practical companion to the [README](../README.md) — the README explains *what* the library does; this doc explains *how* to wire it into your app.

**Reference implementation:** [Pitchcraft](https://github.com/ecoop/pitchcraft) consumes this library in production. Its [`app_state.py`](https://github.com/ecoop/pitchcraft/blob/main/app_state.py) is the canonical adoption pattern; the file pointers throughout this doc are all in that repo.

---

## Install

```bash
pip install guest-auth
```

Requires Python 3.11+. The only runtime dependency is `starlette` (which every ASGI host already has), so the install stays lean.

---

## The dependency-injection pattern

The library **takes zero configuration from the host app.** The `InviteAuthMiddleware` constructor takes explicit arguments; there are no module singletons inside the library, no config imports, no environment-variable reads.

The host app is responsible for:

1. Reading its own config (which env vars, which allowlist source).
2. Constructing the middleware at app-factory time with those values.
3. Exposing anything downstream needs (e.g. `get_current_guest`) through a facade module rather than reaching into `guest_auth` directly if you'd like to keep the seam narrow.

Pitchcraft's [`app_state.py`](https://github.com/ecoop/pitchcraft/blob/main/app_state.py) is the working example for the broader "singleton facade" pattern. The auth-specific slice looks like this:

```python
# app_state.py — the singleton facade
from typing import Optional
from starlette.middleware import Middleware

from guest_auth import InviteAuthMiddleware


_welcome_html: Optional[str] = None


def initialize(settings) -> None:
    """Called from main.py at startup, before the FastAPI app factory."""
    global _welcome_html
    _welcome_html = _render_welcome_html(settings.welcome_md_path)


def guest_auth_middleware(settings) -> Middleware:
    """Return the middleware ready to slot into ``FastAPI(middleware=[...])``
    or ``app.add_middleware(...)``. Read once at app-factory time.
    """
    return Middleware(
        InviteAuthMiddleware,
        config=settings,
        welcome_html=_welcome_html,
    )


def _render_welcome_html(md_path) -> str:
    """Host-side: render your welcome copy. The library takes pre-rendered
    HTML so it stays free of markdown / filesystem dependencies.
    """
    ...
```

Then in `main.py`:

```python
# main.py — order matters
from config import settings

import app_state
app_state.initialize(settings)  # BEFORE the FastAPI app is built

from fastapi import FastAPI
app = FastAPI()
app.add_middleware(
    InviteAuthMiddleware,
    config=settings,
    welcome_html=app_state._welcome_html,
)
```

Consumers of the identity import `get_current_guest` directly — it's a plain function that reads a `ContextVar`, safe to call from anywhere:

```python
from guest_auth import get_current_guest

def some_endpoint():
    guest = get_current_guest()
    if guest is not None:
        log.info("request from %s (%s)", guest.recipient, guest.token[:8])
```

---

## Gotchas

### 1. Pure-ASGI is load-bearing — do NOT use `BaseHTTPMiddleware`

The middleware is a pure ASGI class (`__call__(scope, receive, send)`), not `BaseHTTPMiddleware` and not `@app.middleware("http")`. This is not a stylistic choice.

Both `BaseHTTPMiddleware` and the `@app.middleware` decorator run the downstream app in a **separate anyio task**. Any `ContextVar` set inside their `dispatch` reaches the endpoint *only if the endpoint happens to run in the same task*. FastAPI's sync (`def`) endpoints run in Starlette's threadpool — a different task — and the `ContextVar` propagation there is unreliable.

`InviteAuthMiddleware` sets the identity `ContextVar` from the same task the endpoint runs in, so it propagates into the threadpool along with the copy anyio takes when it launches the sync worker. If you find yourself wrapping this middleware in another `BaseHTTPMiddleware` layer, understand that any ContextVars *that layer* sets won't reach sync endpoints — that's a Starlette-level issue independent of this library.

The generic sibling `PathScopedContextVarMiddleware` follows the same pattern for the same reason.

### 2. Init order — `initialize()` before FastAPI app-factory

If you're using a facade like the one above, call `app_state.initialize(...)` **before** the FastAPI app is constructed and its routers imported. Once routers are imported, decorators have already fired; anything they closed over (config values, middleware factories) is snapshotted.

The middleware itself is fine either way — it reads config at request time — but any *rendering* your facade does at `initialize()` (like the welcome HTML) needs to be ready before whoever calls it.

### 3. Config is read at request time, not construction time

The middleware reads `config.demo_mode` and `config.invite_tokens` on every request via attribute lookup. Two implications:

- **Tests** can `monkeypatch.setattr(settings, "invite_tokens", {"tok_x": "Test"})` on a live app and the next request picks it up.
- **Production hot-swap**: if your config source is mutable (a reload signal, a file watcher), pushing a new allowlist value into the same instance flips the gate without a restart.

If you specifically want construction-time snapshotting instead, wrap your config in an immutable dataclass and pass a fresh instance.

### 4. Cookie name is a module constant, not per-instance

`guest_auth.COOKIE_NAME = "guest_session"`. If you run two apps on the same origin and want per-app cookies, you need one of:

- Deploy them on different domains / paths so cookies don't collide.
- Fork the library and change the constant (there is no constructor arg for it in v0.1.0 — that's a candidate for v0.2).

---

## Claims (role / scopes)

guest-auth can attach authorization **claims** to the identity. It owns their
*structure* and never interprets them: any string is a role, any strings are
scopes. Whether `level5` may rebuild an index is policy, and policy is yours.

Pass a `claims_resolver` — `token -> GuestClaims | None`, sync or async:

```python
from guest_auth import GuestClaims, InviteAuthMiddleware


def resolve_claims(token: str) -> GuestClaims:
    return GuestClaims(
        role=resolve_role(token),                       # your store
        scopes=tuple(resolve_allowed_domains(token)),   # your store
    )


app.add_middleware(
    InviteAuthMiddleware,
    config=settings,
    claims_resolver=resolve_claims,
)
```

Downstream, `get_current_guest().role` / `.scopes` carry the result.

Three rules worth internalising before you wire one up:

### `role=None` means *unresolved*, not *unprivileged*

You get `None` when no resolver is configured, when the resolver had nothing for
the token, **and when the resolver failed**. A policy layer must map `None` to its
own floor explicitly. Reading `None` as a permissive default fails open during
exactly the outage that produced it.

### Resolution failure is soft

If the resolver raises, guest-auth logs it and serves the request with empty
claims. The token is still valid — the gate just doesn't know what it may do.
A claim store hiccup degrades authorization, it never 401s an invited tester.
This is deliberate, and it is why the rule above matters.

### Caching is yours

The resolver runs on every authenticated request. If it reads a bucket, a file,
or a database, cache inside the resolver — guest-auth has no opinion about
invalidation and won't grow one. A sync resolver runs in Starlette's threadpool,
so a blocking read costs that request its latency but never stalls the event
loop for everyone else.

---

## Constructor reference

### `InviteAuthMiddleware`

```python
InviteAuthMiddleware(
    app,                              # ASGI app being wrapped
    config,                           # object exposing demo_mode + invite_tokens
    *,
    welcome_html: str | None = None,  # pre-rendered 401 / welcome body
    claims_resolver: ClaimsResolver | None = None,  # token -> GuestClaims
)
```

| Arg | Type | Purpose |
|---|---|---|
| `app` | `ASGIApp` | Downstream app the middleware wraps. Populated automatically when using `app.add_middleware()`. |
| `config` | `GuestAuthConfig` | Any object with `demo_mode: bool` and `invite_tokens: Mapping[str, str]`. Attribute-read per request. |
| `welcome_html` | `str \| None` | Pre-rendered HTML body injected verbatim into the library's page chrome. `None` uses a minimal built-in fallback. |
| `claims_resolver` | `ClaimsResolver \| None` | `token -> GuestClaims \| None`, sync or async. Called once per authenticated request to attach `role` / `scopes`. `None` means every identity carries no claims. See [Claims](#claims-role--scopes). |

### `PathScopedContextVarMiddleware`

```python
PathScopedContextVarMiddleware(
    app,
    *,
    pattern: re.Pattern | str,             # matched against scope["path"]
    contextvar: ContextVar[Any],           # target var to publish onto
    extractor: Callable[[re.Match], Any] = lambda m: m.group(1),
)
```

| Arg | Type | Purpose |
|---|---|---|
| `pattern` | `re.Pattern \| str` | Matched with `.match()` — anchor with `^` for a prefix match. String is compiled internally. |
| `contextvar` | `ContextVar[Any]` | Value stored on match; `None` on non-match. Reset after each request. |
| `extractor` | `Callable` | How to turn the `re.Match` into the stored value. Default: first capture group. |

### `GuestIdentity`

```python
@dataclass(frozen=True)
class GuestIdentity:
    token: str                          # the credential itself
    recipient: str                      # human-readable label from the allowlist
    role: str | None = None             # claim; None means UNRESOLVED
    scopes: tuple[str, ...] | None = None   # claim; None means no scopes claim
```

Frozen *and hashable* — consumers key per-recipient counters off it. That is why
`scopes` is a `tuple` and not a `list`: one mutable field turns `hash()` into a
`TypeError`, and it would only fire on requests that actually carry claims.

### `GuestClaims`

```python
@dataclass(frozen=True)
class GuestClaims:
    role: str | None = None
    scopes: tuple[str, ...] | None = None
```

What a resolver returns. Returning `None` instead is equivalent to `GuestClaims()`.

### Module-level constants + functions

| Name | Purpose |
|---|---|
| `COOKIE_NAME = "guest_session"` | Cookie the middleware sets and reads. |
| `COOKIE_MAX_AGE = 60 * 60 * 24 * 30` | 30 days, in seconds. Convenience lifetime, not token expiry. |
| `get_current_guest() -> GuestIdentity \| None` | Read the current request's identity, or `None` outside a request or when demo mode is off. |
| `ClaimsResolver` | Type alias for the resolver callable: `Callable[[str], GuestClaims \| None \| Awaitable[...]]`. |

---

## Reference implementation: Pitchcraft

Files worth skimming, in priority order:

1. [`app_state.py`](https://github.com/ecoop/pitchcraft/blob/main/app_state.py) — singleton facade pattern.
2. [`api/main.py`](https://github.com/ecoop/pitchcraft/blob/main/api/main.py) — how the middleware slots into the FastAPI app-factory.
3. [`api/observability.py`](https://github.com/ecoop/pitchcraft/blob/main/api/observability.py) — a consumer of `get_current_guest()` in the request path.

---

## Getting help

For questions on the adoption pattern that aren't covered here: open an issue on this repo, or point at the Pitchcraft reference files above — they're the working ground truth.
