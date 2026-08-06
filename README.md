# guest-auth

**Not a replacement for real authentication.** A static-allowlist invite-token gate for pre-production demos and invite-only previews. Pure-ASGI middleware that plugs into any Starlette / FastAPI app in ~5 lines.

Give a tester a link like `https://your-app.example.com/?token=tok_abc123`; the middleware validates the token against an allowlist you own, exchanges it for an `httpOnly` cookie, and attaches an identity (`token` + human-readable `recipient` label) to the request via a `ContextVar` that reaches sync endpoints in the threadpool as well.

The library was extracted from [Pitchcraft](https://github.com/ecoop/pitchcraft) and is consumed there in production; [Rulebook](https://github.com/ecoop/rulebook) and JobScout are scheduled to adopt it.

**Adopting this in a new app?** See [`docs/integration.md`](docs/integration.md) for the DI pattern, the `app_state.py` template, gotchas (init order, pure-ASGI vs `BaseHTTPMiddleware`, ContextVar propagation), and the constructor reference.

---

## What this is NOT

Naming a library `*-auth` invites expectations it doesn't meet. To be explicit:

- **No password handling, no MFA, no OAuth / OIDC, no account lifecycle.** The credential is an opaque token you generate and hand to a tester.
- **No token rotation, expiry, revocation-list, or signed cookies.** The cookie is httpOnly + Secure + SameSite=Lax with a 30-day convenience lifetime; revoking access means removing the token from the allowlist and redeploying.
- **No rate limiting.** Compose one separately (e.g. [`llm-guardrails`](https://github.com/ecoop/llm-guardrails) ships an IP rate limiter).
- **Not audited for adversarial threat models.** This is a gate to keep pre-production URLs off the open web and attribute per-tester activity, not a substitute for real identity infrastructure. If you're gating production PII or payment flows, use something else.

The value the library provides — a well-scoped ASGI middleware that publishes a per-request identity ContextVar that reaches sync endpoints — is genuinely useful and hard to get right (the "pure-ASGI vs `BaseHTTPMiddleware`" trap is subtle). Everything above is deferred, not planned.

---

## Install

```bash
pip install guest-auth
```

Requires Python 3.11+. The only runtime dependency is `starlette`, which any ASGI host already has.

---

## Quick example

```python
from dataclasses import dataclass, field
from fastapi import FastAPI
from guest_auth import InviteAuthMiddleware, get_current_guest


@dataclass
class Settings:
    demo_mode: bool = True
    invite_tokens: dict = field(
        default_factory=lambda: {"tok_abc123": "Jane Tester"}
    )


settings = Settings()
app = FastAPI()


@app.get("/")
def home():
    guest = get_current_guest()
    return {"welcome": guest.recipient if guest else "anonymous"}


app.add_middleware(
    InviteAuthMiddleware,
    config=settings,
    # Optional — pre-rendered HTML for the 401 / welcome page.
    # Omit to use the built-in "This site is currently invite-only." body.
    welcome_html="<h1>Preview build</h1><p>Ask jane@example.com for a link.</p>",
)
```

Now:

- `GET /?token=tok_abc123` → 302 to `/`, sets `guest_session` cookie.
- `GET /` with the cookie → returns `{"welcome": "Jane Tester"}`.
- `GET /` without a cookie → 401 with the welcome page.
- Flip `settings.demo_mode = False` → gate becomes a complete pass-through with no restart.

---

## Core concepts

### `GuestAuthConfig` (Protocol)

The middleware takes a `config` object that satisfies:

```python
class GuestAuthConfig(Protocol):
    demo_mode: bool
    invite_tokens: Mapping[str, str]  # token → recipient label
```

Both attributes are read at request time, so mutating a live `config` instance (a pydantic `BaseSettings`, a dataclass, whatever) takes effect on the next request without rebuilding the middleware.

### `GuestIdentity` + `get_current_guest()`

On a successful cookie match, the middleware sets a request-scoped `ContextVar` with `GuestIdentity(token=..., recipient=...)`. Anywhere downstream — sync or async, including code paths in Starlette's threadpool — `get_current_guest()` returns it or `None`.

Because the middleware is pure-ASGI (not `BaseHTTPMiddleware`), the `ContextVar` survives into the threadpool that runs `def` (sync) endpoints. See the [integration doc](docs/integration.md#gotchas) for why this matters.

### `PathScopedContextVarMiddleware` (bonus)

An adjacent generic that ships in the same package: match a regex against `scope["path"]`, publish an extracted value on a caller-supplied `ContextVar` for the duration of the request. Same pure-ASGI rationale as the auth middleware. Use for `/api/things/{id}/…` style path-scoped ContextVars (session IDs, tenant IDs, whatever).

---

## Development

```bash
git clone https://github.com/ecoop/guest-auth
cd guest-auth
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

CI runs on Python 3.11 and 3.12 via [GitHub Actions](.github/workflows/ci.yml).

## Versioning

Currently `v0.1.1`. Semver from `v1.0.0` onward; anything before is "shipped but pre-stable API — expect breaking changes."

## Contributing

Issues and pull requests welcome. For substantive changes, open an issue first — this library has a deliberately small surface and staying small is a feature.

## License

MIT. See [LICENSE](LICENSE).

---

_Last updated: 2026-08-06_
