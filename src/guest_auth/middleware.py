# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Invite-token gate for pre-production demos and invite-only previews.

A token-gated entry to the app, active only when the injected config's
``demo_mode`` is on. A tester is handed a one-time link::

    https://your-app.example.com/?token=tok_abc123

The middleware validates the token against the config's ``invite_tokens``
allowlist, exchanges it for an httpOnly cookie, and redirects to a clean
URL. Every subsequent request authenticates via that cookie; the
``?token=`` never reappears in the address bar.

Why a pure-ASGI middleware (not ``BaseHTTPMiddleware`` / the
``@app.middleware`` decorator):
    Both of those run the downstream app in a *separate* anyio task, and
    ``ContextVar`` values set in their ``dispatch`` don't reliably reach
    the endpoint (a long-standing Starlette gotcha). We need the resolved
    identity to be readable at downstream call sites (LLM helpers,
    observability spans, per-request counters), so we set it in a plain
    ASGI middleware — same task, no boundary — and it propagates into the
    (threadpooled, sync) endpoints as well.

Identity propagation:
    On a request with a valid cookie, the middleware sets the
    ``_invite_identity`` ContextVar (defined in ``guest_auth.identity``).
    Downstream code reads it via ``get_current_guest()``. When
    demo mode is off, the middleware is a pass-through and the
    ContextVar stays ``None`` — non-demo deploys are completely
    unaffected.

Config injection:
    The middleware takes a ``GuestAuthConfig`` at construction — any
    object exposing ``demo_mode: bool`` and ``invite_tokens:
    Mapping[str, str]``. Both fields are read at request time via
    attribute lookup, so tests that ``monkeypatch.setattr`` a mutable
    settings instance still take effect on the live app.

Welcome-page injection:
    The 401 / welcome page body is injected as pre-rendered HTML at
    construction (``welcome_html=...``). The library has no opinion on
    how the host authors it — plain HTML, markdown, Jinja, whatever.
    Omitting the argument uses a minimal built-in fallback with a
    generic invite-only message; a host that wants a real welcome
    surface passes one in. This keeps the library free of any markdown
    or filesystem dependencies.

Out of scope: token expiry/rotation, signed cookies,
multi-token-per-recipient, an admin UI. The cookie ``max_age`` below is a
convenience lifetime, not token expiry — revoking access still means
editing the allowlist and redeploying.
"""

import logging
from collections.abc import Mapping
from typing import Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from guest_auth.identity import GuestIdentity, _invite_identity

_log = logging.getLogger(__name__)


class GuestAuthConfig(Protocol):
    """Structural type for the config object the middleware needs.

    Any object exposing the two attributes below satisfies this — a
    pydantic ``BaseSettings`` instance, a plain dataclass, an
    ``argparse.Namespace``, whatever. Attribute lookup happens at
    request time so ``monkeypatch.setattr`` on a mutable instance takes
    effect without rebuilding the middleware.
    """

    @property
    def demo_mode(self) -> bool: ...

    @property
    def invite_tokens(self) -> Mapping[str, str]: ...


# Cookie name + lifetime. httpOnly + Secure + SameSite=Lax are set at
# write time below. 30 days is a tester-convenience lifetime so a browser
# restart doesn't force re-clicking the invite link; it is NOT token
# expiry (that's explicitly deferred — see module docstring).
COOKIE_NAME = "guest_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, in seconds


# Minimal, self-contained page chrome. Standalone (not the SPA), so it
# can't share the host app's theme. Light/dark aware via
# prefers-color-scheme. Host-supplied ``welcome_html`` is injected into
# ``{body}`` verbatim.
_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      Helvetica, Arial, sans-serif;
    line-height: 1.65;
    max-width: 38rem;
    margin: 0 auto;
    padding: 3rem 1.25rem 4rem;
    color: #1a1a1a;
    background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #121212; }}
    a {{ color: #8ab4f8; }}
  }}
  a {{ color: #1a56db; }}
  h1, h2 {{ line-height: 1.25; }}
  h1 {{ margin-top: 0; }}
  h2 {{ margin-top: 2rem; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""

# Minimal built-in body used when the host doesn't supply
# ``welcome_html``. The gate page is on the critical path — first
# contact for anyone hitting the domain — so it must always render
# *something*, even if the host forgot to configure a welcome message.
_FALLBACK_BODY = (
    "<h1>Welcome</h1>"
    "<p>This site is currently invite-only.</p>"
)


def _clean_location(request: Request) -> str:
    """Return a path-relative redirect target with ``token`` stripped.

    Path-relative (not absolute) so the redirect survives an
    HTTPS-terminating proxy without leaking an ``http://`` scheme. Any
    non-``token`` query params are preserved.
    """
    clean = request.url.remove_query_params("token")
    location = clean.path or "/"
    if clean.query:
        location = f"{location}?{clean.query}"
    return location


class InviteAuthMiddleware:
    """Pure-ASGI gate: validate invite token, exchange for cookie, attach identity.

    Behaviour per request (only when ``config.demo_mode`` is true; a
    no-op pass-through otherwise):

      - ``?token=X`` present and in the allowlist → set the
        ``guest_session`` cookie and 302-redirect to the clean URL.
      - ``?token=X`` present but unknown → 401 friendly page.
      - Valid ``guest_session`` cookie → set the identity ContextVar
        and serve the request normally.
      - No / invalid cookie → 401 friendly page.

    Args:
        app: The wrapped ASGI application.
        config: A ``GuestAuthConfig`` (see the Protocol above). Read at
            request time so ``monkeypatch.setattr`` on a mutable
            instance takes effect on the live app.
        welcome_html: Pre-rendered HTML body for the 401 / welcome
            page. Injected verbatim into the library's page chrome.
            ``None`` uses a minimal built-in fallback; hosts that want
            a real welcome message pass their own rendered HTML in.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: GuestAuthConfig,
        *,
        welcome_html: str | None = None,
    ) -> None:
        self.app = app
        self._config = config
        body = welcome_html if welcome_html is not None else _FALLBACK_BODY
        # Render the full page once at construction: neither the chrome
        # nor the host-supplied body changes per-request, so there is
        # no reason to re-format the template on every 401.
        self._page = _PAGE.format(body=body)

    def _unauthorized_response(self) -> HTMLResponse:
        """Build the friendly 401 / welcome page for un-invited visitors.

        Dual-purpose: the welcome page for someone landing on the bare
        URL (no token, no cookie) AND the "your invite link didn't
        work" page if the token or cookie is rejected.
        """
        return HTMLResponse(content=self._page, status_code=401)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only gate HTTP requests; let lifespan/websocket pass untouched.
        # When demo mode is off the gate is a complete no-op.
        if scope["type"] != "http" or not self._config.demo_mode:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        allowlist = self._config.invite_tokens

        # 1. Token-in-URL → exchange for a cookie, then redirect clean.
        url_token = request.query_params.get("token")
        if url_token is not None:
            if url_token in allowlist:
                response = RedirectResponse(_clean_location(request), status_code=302)
                response.set_cookie(
                    COOKIE_NAME,
                    url_token,
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path="/",
                )
                await response(scope, receive, send)
                return
            await self._unauthorized_response()(scope, receive, send)
            return

        # 2. Cookie auth → attach identity for the duration of the request.
        cookie_token = request.cookies.get(COOKIE_NAME)
        recipient = allowlist.get(cookie_token) if cookie_token else None
        if recipient is not None:
            reset = _invite_identity.set(
                GuestIdentity(token=cookie_token, recipient=recipient)
            )
            try:
                await self.app(scope, receive, send)
            finally:
                _invite_identity.reset(reset)
            return

        # 3. No valid credential → friendly 401.
        await self._unauthorized_response()(scope, receive, send)
